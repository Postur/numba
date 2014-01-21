"""
Define typing templates
"""
from __future__ import print_function, division, absolute_import
import math
import sys
import numpy
from numba import types
from numba.config import PYVERSION


class Signature(object):
    __slots__ = 'return_type', 'args', 'recvr'

    def __init__(self, return_type, args, recvr):
        self.return_type = return_type
        self.args = args
        self.recvr = recvr

    def __hash__(self):
        return hash(self.args)

    def __eq__(self, other):
        if isinstance(other, Signature):
            return (self.args == other.args and
                    self.recvr == other.recvr)

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return "%s -> %s" % (self.args, self.return_type)

    @property
    def is_method(self):
        return self.recvr is not None


def signature(return_type, *args, **kws):
    recvr = kws.pop('recvr', None)
    assert not kws
    return Signature(return_type, args, recvr=recvr)


def _uses_downcast(dists):
    for d in dists:
        if d < 0:
            return True
    return False


def _sum_downcast(dists):
    c = 0
    for d in dists:
        if d < 0:
            c += abs(d)
    return c


class Rating(object):
    __slots__ = 'promote', 'safe_convert', "unsafe_convert"

    def __init__(self):
        self.promote = 0
        self.safe_convert = 0
        self.unsafe_convert = 0

    def astuple(self):
        """Returns a tuple suitable for comparing with the worse situation
        start first.
        """
        return (self.unsafe_convert, self.safe_convert, self.promote)


class FunctionTemplate(object):
    def __init__(self, context):
        self.context = context

    def _select(self, cases, args, kws):
        selected = self._resolve_overload(cases, args, kws)
        return selected

    def _resolve_overload(self, cases, args, kws):
        assert not kws, "Keyword arguments are not supported, yet"
        # Rate each cases
        candids = []
        ratings = []
        for case in cases:
            if len(args) == len(case.args):
                rate = Rating()
                for actual, formal in zip(args, case.args):
                    by = self.context.type_compatibility(actual, formal)
                    if by is None:
                        break

                    if by == 'promote':
                        rate.promote += 1
                    elif by == 'safe':
                        rate.safe_convert += 1
                    elif by == 'unsafe':
                        rate.unsafe_convert += 1
                    elif by == 'exact':
                        pass
                    else:
                        raise Exception("unreachable", by)

                else:
                    ratings.append(rate.astuple())
                    candids.append(case)
        # Find the best case
        ordered = sorted(zip(ratings, candids), key=lambda i: i[0])
        if ordered:
            if len(ordered) > 1:
                (first, case1), (second, case2) = ordered[:2]
                if first == second:
                    ambiguous = []
                    for rate, case in ordered:
                        if rate == first:
                            ambiguous.append(str(case))
                    args = (self.key, args, '\n'.join(ambiguous))
                    msg = "Ambiguous overloading for %s %s\n%s" % args
                    raise TypeError(msg)

            return ordered[0][1]


class AbstractTemplate(FunctionTemplate):
    """
    Defines method ``generic(self, args, kws)`` which compute a possible
    signature base on input types.  The signature does not have to match the
    input types. It is compared against the input types afterwards.
    """

    def apply(self, args, kws):
        generic = getattr(self, "generic")
        sig = generic(args, kws)
        if sig:
            cases = [sig]
            return self._select(cases, args, kws)


class ConcreteTemplate(FunctionTemplate):
    """
    Defines attributes "cases" as a list of signature to match against the
    given input types.
    """

    def apply(self, args, kws):
        cases = getattr(self, 'cases')
        assert cases
        return self._select(cases, args, kws)


class AttributeTemplate(object):
    def __init__(self, context):
        self.context = context

    def resolve(self, value, attr):
        fn = getattr(self, "resolve_%s" % attr, None)
        if fn is None:
            raise NotImplementedError(value, attr)
        return fn(value)


class ClassAttrTemplate(AttributeTemplate):
    def __init__(self, context, key, clsdict):
        super(ClassAttrTemplate, self).__init__(context)
        self.key = key
        self.clsdict = clsdict

    def resolve(self, value, attr):
        return self.clsdict[attr]


# -----------------------------------------------------------------------------

BUILTINS = []
BUILTIN_ATTRS = []
BUILTIN_GLOBALS = []


def builtin(template):
    if issubclass(template, AttributeTemplate):
        BUILTIN_ATTRS.append(template)
    else:
        BUILTINS.append(template)
    return template


def builtin_global(v, t):
    BUILTIN_GLOBALS.append((v, t))


builtin_global(range, types.range_type)
if PYVERSION < (3, 0):
    builtin_global(xrange, types.range_type)
builtin_global(len, types.len_type)
builtin_global(slice, types.slice_type)
builtin_global(abs, types.abs_type)
builtin_global(print, types.print_type)


@builtin
class Print(ConcreteTemplate):
    key = types.print_type
    intcases = [signature(types.none, ty) for ty in types.integer_domain]
    realcases = [signature(types.none, ty) for ty in types.real_domain]
    cases = intcases + realcases


@builtin
class Abs(ConcreteTemplate):
    key = types.abs_type
    cases = [signature(ty, ty) for ty in types.signed_domain]


@builtin
class Slice(ConcreteTemplate):
    key = types.slice_type
    cases = [
        signature(types.slice2_type, types.intp, types.intp),
        signature(types.slice3_type, types.intp, types.intp, types.intp),
    ]


@builtin
class Range(ConcreteTemplate):
    key = types.range_type
    cases = [
        signature(types.range_state32_type, types.int32),
        signature(types.range_state32_type, types.int32, types.int32,
                  types.int32),
        signature(types.range_state64_type, types.int64),
        signature(types.range_state64_type, types.int64, types.int64),
        signature(types.range_state64_type, types.int64, types.int64,
                  types.int64),
    ]


@builtin
class GetIter(ConcreteTemplate):
    key = "getiter"
    cases = [
        signature(types.range_iter32_type, types.range_state32_type),
        signature(types.range_iter64_type, types.range_state64_type),
    ]


@builtin
class GetIterUniTuple(AbstractTemplate):
    key = "getiter"

    def generic(self, args, kws):
        assert not kws
        [tup] = args
        if isinstance(tup, types.UniTuple):
            return signature(types.UniTupleIter(tup), tup)


@builtin
class IterNext(ConcreteTemplate):
    key = "iternext"
    cases = [
        signature(types.int32, types.range_iter32_type),
        signature(types.int64, types.range_iter64_type),
    ]


@builtin
class IterNextSafe(AbstractTemplate):
    key = "iternextsafe"

    def generic(self, args, kws):
        assert not kws
        [tupiter] = args
        if isinstance(tupiter, types.UniTupleIter):
            return signature(tupiter.unituple.dtype, tupiter)


@builtin
class IterValid(ConcreteTemplate):
    key = "itervalid"
    cases = [
        signature(types.boolean, types.range_iter32_type),
        signature(types.boolean, types.range_iter64_type),
    ]


class BinOp(ConcreteTemplate):
    cases = [
        signature(types.uint8, types.uint8, types.uint8),
        signature(types.uint16, types.uint16, types.uint16),
        signature(types.uint32, types.uint32, types.uint32),
        signature(types.uint64, types.uint64, types.uint64),

        signature(types.int8, types.int8, types.int8),
        signature(types.int16, types.int16, types.int16),
        signature(types.int32, types.int32, types.int32),
        signature(types.int64, types.int64, types.int64),

        signature(types.float32, types.float32, types.float32),
        signature(types.float64, types.float64, types.float64),

        signature(types.complex64, types.complex64, types.complex64),
        signature(types.complex128, types.complex128, types.complex128),
    ]


@builtin
class BinOpAdd(BinOp):
    key = "+"


@builtin
class BinOpSub(BinOp):
    key = "-"


@builtin
class BinOpMul(BinOp):
    key = "*"


@builtin
class BinOpDiv(BinOp):
    key = "/?"


@builtin
class BinOpMod(ConcreteTemplate):
    key = "%"

    cases = [
        signature(types.uint8, types.uint8, types.uint8),
        signature(types.uint16, types.uint16, types.uint16),
        signature(types.uint32, types.uint32, types.uint32),
        signature(types.uint64, types.uint64, types.uint64),

        signature(types.int8, types.int8, types.int8),
        signature(types.int16, types.int16, types.int16),
        signature(types.int32, types.int32, types.int32),
        signature(types.int64, types.int64, types.int64),

        signature(types.float32, types.float32, types.float32),
        signature(types.float64, types.float64, types.float64),
    ]


@builtin
class BinOpTrueDiv(ConcreteTemplate):
    key = "/"

    cases = [
        signature(types.float64, types.uint8, types.uint8),
        signature(types.float64, types.uint16, types.uint16),
        signature(types.float64, types.uint32, types.uint32),
        signature(types.float64, types.uint64, types.uint64),

        signature(types.float64, types.int8, types.int8),
        signature(types.float64, types.int16, types.int16),
        signature(types.float64, types.int32, types.int32),
        signature(types.float64, types.int64, types.int64),


        signature(types.float32, types.float32, types.float32),
        signature(types.float64, types.float64, types.float64),

        signature(types.complex64, types.complex64, types.complex64),
        signature(types.complex128, types.complex128, types.complex128),

    ]

@builtin
class BinOpFloorDiv(ConcreteTemplate):
    key = "//"
    cases = [
        signature(types.int8, types.int8, types.int8),
        signature(types.int16, types.int16, types.int16),
        signature(types.int32, types.int32, types.int32),
        signature(types.int64, types.int64, types.int64),

        signature(types.uint8, types.uint8, types.uint8),
        signature(types.uint16, types.uint16, types.uint16),
        signature(types.uint32, types.uint32, types.uint32),
        signature(types.uint64, types.uint64, types.uint64),

        signature(types.int32, types.float32, types.float32),
        signature(types.int64, types.float64, types.float64),
    ]


@builtin
class BinOpPower(ConcreteTemplate):
    key = "**"
    cases = [
        signature(types.float64, types.float64, types.uint8),
        signature(types.float64, types.float64, types.uint16),
        signature(types.float64, types.float64, types.uint32),
        signature(types.float64, types.float64, types.uint64),

        signature(types.float64, types.float64, types.int8),
        signature(types.float64, types.float64, types.int16),
        signature(types.float64, types.float64, types.int32),
        signature(types.float64, types.float64, types.int64),
        signature(types.float32, types.float32, types.float32),
        signature(types.float64, types.float64, types.float64),

        signature(types.complex64, types.complex64, types.complex64),
        signature(types.complex128, types.complex128, types.complex128),
    ]


class CmpOp(ConcreteTemplate):
    cases = [
        signature(types.boolean, types.uint8, types.uint8),
        signature(types.boolean, types.uint16, types.uint16),
        signature(types.boolean, types.uint32, types.uint32),
        signature(types.boolean, types.uint64, types.uint64),

        signature(types.boolean, types.int8, types.int8),
        signature(types.boolean, types.int16, types.int16),
        signature(types.boolean, types.int32, types.int32),
        signature(types.boolean, types.int64, types.int64),

        signature(types.boolean, types.float32, types.float32),
        signature(types.boolean, types.float64, types.float64),
    ]


@builtin
class CmpOpLt(CmpOp):
    key = '<'


@builtin
class CmpOpLe(CmpOp):
    key = '<='


@builtin
class CmpOpGt(CmpOp):
    key = '>'


@builtin
class CmpOpGe(CmpOp):
    key = '>='


@builtin
class CmpOpEq(CmpOp):
    key = '=='


@builtin
class CmpOpNe(CmpOp):
    key = '!='


def normalize_index(index):
    if isinstance(index, types.UniTuple):
        return types.UniTuple(types.intp, index.count)

    elif index == types.slice3_type:
        return types.slice3_type

    elif index == types.slice2_type:
        return types.slice2_type

    else:
        return types.intp


@builtin
class GetItemUniTuple(AbstractTemplate):
    key = "getitem"

    def generic(self, args, kws):
        tup, idx = args
        if isinstance(tup, types.UniTuple):
            return signature(tup.dtype, tup, normalize_index(idx))


@builtin
class GetItemArray(AbstractTemplate):
    key = "getitem"

    def generic(self, args, kws):
        assert not kws
        ary, idx = args
        if not isinstance(ary, types.Array):
            return

        idx = normalize_index(idx)
        if idx in (types.slice2_type, types.slice3_type):
            res = ary.copy(layout='A')
        elif isinstance(idx, types.UniTuple):
            if ary.ndim > len(idx):
                return
            elif ary.ndim < len(idx):
                return
            else:
                res = ary.dtype
        elif idx == types.intp:
            if ary.ndim != 1:
                return
            res = ary.dtype
        else:
            raise Exception("unreachable")

        return signature(res, ary, idx)


@builtin
class SetItemArray(AbstractTemplate):
    key = "setitem"

    def generic(self, args, kws):
        assert not kws
        ary, idx, val = args
        if isinstance(ary, types.Array):
            return signature(types.none, ary, normalize_index(idx), ary.dtype)


@builtin
class LenArray(AbstractTemplate):
    key = types.len_type

    def generic(self, args, kws):
        assert not kws
        (ary,) = args
        if isinstance(ary, types.Array):
            return signature(types.intp, ary)

#-------------------------------------------------------------------------------

@builtin
class ArrayAttribute(AttributeTemplate):
    key = types.Array

    def resolve_shape(self, ary):
        return types.UniTuple(types.intp, ary.ndim)

    def resolve_flatten(self, ary):
        return types.Method(Array_flatten, ary)


class Array_flatten(AbstractTemplate):
    key = "array.flatten"

    def generic(self, args, kws):
        assert not args
        assert not kws
        this = self.this
        if this.layout == 'C':
            resty = this.copy(ndim=1)
            return signature(resty, recvr=this)


@builtin
class CmpOpEqArray(AbstractTemplate):
    key = '=='

    def generic(self, args, kws):
        assert not kws
        [va, vb] = args
        if isinstance(va, types.Array) and va == vb:
            return signature(va.copy(dtype=types.boolean), va, vb)


#-------------------------------------------------------------------------------
class ComplexAttribute(AttributeTemplate):

    def resolve_real(self, ty):
        return self.innertype

    def resolve_imag(self, ty):
        return self.innertype


@builtin
class Complex64Attribute(ComplexAttribute):
    key = types.complex64
    innertype = types.float32


@builtin
class Complex128Attribute(ComplexAttribute):
    key = types.complex128
    innertype = types.float64

#-------------------------------------------------------------------------------

@builtin
class MathModuleAttribute(AttributeTemplate):
    key = types.Module(math)

    def resolve_fabs(self, mod):
        return types.Function(Math_fabs)

    def resolve_exp(self, mod):
        return types.Function(Math_exp)

    def resolve_sqrt(self, mod):
        return types.Function(Math_sqrt)

    def resolve_log(self, mod):
        return types.Function(Math_log)

    def resolve_sin(self, mod):
        return types.Function(Math_sin)

    def resolve_cos(self, mod):
        return types.Function(Math_cos)

    def resolve_tan(self, mod):
        return types.Function(Math_tan)

    def resolve_sinh(self, mod):
        return types.Function(Math_sinh)

    def resolve_cosh(self, mod):
        return types.Function(Math_cosh)

    def resolve_tanh(self, mod):
        return types.Function(Math_tanh)

    def resolve_asin(self, mod):
        return types.Function(Math_asin)

    def resolve_acos(self, mod):
        return types.Function(Math_acos)

    def resolve_atan(self, mod):
        return types.Function(Math_atan)

    def resolve_asinh(self, mod):
        return types.Function(Math_asinh)

    def resolve_acosh(self, mod):
        return types.Function(Math_acosh)

    def resolve_atanh(self, mod):
        return types.Function(Math_atanh)


class Math_unary(ConcreteTemplate):
    cases = [
        signature(types.float64, types.int64),
        signature(types.float64, types.uint64),
        signature(types.float32, types.float32),
        signature(types.float64, types.float64),
    ]


class Math_fabs(Math_unary):
    key = math.fabs


class Math_exp(Math_unary):
    key = math.exp


class Math_sqrt(Math_unary):
    key = math.sqrt


class Math_log(Math_unary):
    key = math.log


class Math_sin(Math_unary):
    key = math.sin


class Math_cos(Math_unary):
    key = math.cos


class Math_tan(Math_unary):
    key = math.tan


class Math_sinh(Math_unary):
    key = math.sinh


class Math_cosh(Math_unary):
    key = math.cosh


class Math_tanh(Math_unary):
    key = math.tanh


class Math_asin(Math_unary):
    key = math.asin


class Math_acos(Math_unary):
    key = math.acos


class Math_atan(Math_unary):
    key = math.atan


class Math_asinh(Math_unary):
    key = math.asinh


class Math_acosh(Math_unary):
    key = math.acosh


class Math_atanh(Math_unary):
    key = math.atanh


builtin_global(math, types.Module(math))
builtin_global(math.fabs, types.Function(Math_fabs))
builtin_global(math.exp, types.Function(Math_exp))
builtin_global(math.sqrt, types.Function(Math_sqrt))
builtin_global(math.log, types.Function(Math_log))
builtin_global(math.sin, types.Function(Math_sin))
builtin_global(math.cos, types.Function(Math_cos))
builtin_global(math.tan, types.Function(Math_tan))
builtin_global(math.sinh, types.Function(Math_sinh))
builtin_global(math.cosh, types.Function(Math_cosh))
builtin_global(math.tanh, types.Function(Math_tanh))
builtin_global(math.asin, types.Function(Math_asin))
builtin_global(math.acos, types.Function(Math_acos))
builtin_global(math.atan, types.Function(Math_atan))
builtin_global(math.asinh, types.Function(Math_asinh))
builtin_global(math.acosh, types.Function(Math_acosh))
builtin_global(math.atanh, types.Function(Math_atanh))

#-------------------------------------------------------------------------------

@builtin
class NumpyModuleAttribute(AttributeTemplate):
    key = types.Module(numpy)

    def resolve_absolute(self, mod):
        return types.Function(Numpy_absolute)

    def resolve_exp(self, mod):
        return types.Function(Numpy_exp)

    def resolve_sin(self, mod):
        return types.Function(Numpy_sin)

    def resolve_cos(self, mod):
        return types.Function(Numpy_cos)

    def resolve_tan(self, mod):
        return types.Function(Numpy_tan)

    def resolve_add(self, mod):
        return types.Function(Numpy_add)

    def resolve_subtract(self, mod):
        return types.Function(Numpy_subtract)

    def resolve_multiply(self, mod):
        return types.Function(Numpy_multiply)

    def resolve_divide(self, mod):
        return types.Function(Numpy_divide)


class Numpy_unary_ufunc(AbstractTemplate):
    def generic(self, args, kws):
        assert not kws
        [inp, out] = args
        if isinstance(inp, types.Array) and isinstance(out, types.Array):
            if inp.dtype != out.dtype:
                # TODO handle differing dtypes
                return
            return signature(out, inp, out)


class Numpy_absolute(Numpy_unary_ufunc):
    key = numpy.absolute


class Numpy_sin(Numpy_unary_ufunc):
    key = numpy.sin


class Numpy_cos(Numpy_unary_ufunc):
    key = numpy.cos


class Numpy_tan(Numpy_unary_ufunc):
    key = numpy.tan


class Numpy_exp(Numpy_unary_ufunc):
    key = numpy.exp


class Numpy_binary_ufunc(AbstractTemplate):
    def generic(self, args, kws):
        assert not kws
        [vx, wy, out] = args
        if (isinstance(vx, types.Array) and isinstance(wy, types.Array) and
                isinstance(out, types.Array)):
            if vx.dtype != wy.dtype and vx.dtype != out.dtype:
                # TODO handle differing dtypes
                return
            return signature(out, vx, wy, out)


class Numpy_add(Numpy_binary_ufunc):
    key = numpy.add


class Numpy_subtract(Numpy_binary_ufunc):
    key = numpy.subtract


class Numpy_multiply(Numpy_binary_ufunc):
    key = numpy.multiply


class Numpy_divide(Numpy_binary_ufunc):
    key = numpy.divide


builtin_global(numpy, types.Module(numpy))
builtin_global(numpy.absolute, types.Function(Numpy_absolute))
builtin_global(numpy.exp, types.Function(Numpy_exp))
builtin_global(numpy.sin, types.Function(Numpy_sin))
builtin_global(numpy.cos, types.Function(Numpy_cos))
builtin_global(numpy.tan, types.Function(Numpy_tan))
builtin_global(numpy.add, types.Function(Numpy_add))
builtin_global(numpy.subtract, types.Function(Numpy_subtract))
builtin_global(numpy.multiply, types.Function(Numpy_multiply))
builtin_global(numpy.divide, types.Function(Numpy_divide))


