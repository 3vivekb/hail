from typing import *

import hail as hl
from hail.expr.expressions import Expression, to_expr, ExpressionException, \
    unify_all, Indices, Aggregation, unify_types
from hail.expr.expressions.expression_typecheck import *
from hail.expr.types import *
from hail.ir import *
from hail.typecheck import *
from hail.utils.java import *
from hail.utils.linkedlist import LinkedList
from hail.utils.misc import get_nice_field_error, get_nice_attr_error
from hail.genetics.reference_genome import reference_genome_type

import tempfile
import numpy as np


class CollectionExpression(Expression):
    """Expression of type :class:`.tarray` or :class:`.tset`

    >>> a = hl.literal([1, 2, 3, 4, 5])

    >>> s3 = hl.literal({'Alice', 'Bob', 'Charlie'})
    """


    def _filter_missing_method(self, filter_missing: bool, name: str, ret_type: HailType, *args):
        collection = self
        if filter_missing:
            collection = self.filter(hl.is_defined)
        return collection._method(name, ret_type, *args)

    @typecheck_method(f=func_spec(1, expr_bool))
    def any(self, f):
        """Returns ``True`` if `f` returns ``True`` for any element.

        Examples
        --------

        >>> hl.eval(a.any(lambda x: x % 2 == 0))
        True

        >>> hl.eval(s3.any(lambda x: x[0] == 'D'))
        False

        Notes
        -----
        This method always returns ``False`` for empty collections.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.BooleanExpression`.
            ``True`` if `f` returns ``True`` for any element, ``False`` otherwise.
        """
        f2 = lambda accum, elt: accum | f(elt)
        return hl.array(self).fold(f2, False)

    @typecheck_method(f=func_spec(1, expr_bool))
    def filter(self, f):
        """Returns a new collection containing elements where `f` returns ``True``.

        Examples
        --------

        >>> hl.eval(a.filter(lambda x: x % 2 == 0))
        [2, 4]

        >>> hl.eval(s3.filter(lambda x: ~(x[-1] == 'e')))  # doctest: +SKIP_OUTPUT_CHECK
        {'Bob'}

        Notes
        -----
        Returns a same-type expression; evaluated on a :class:`.SetExpression`, returns a
        :class:`.SetExpression`. Evaluated on an :class:`.ArrayExpression`,
        returns an :class:`.ArrayExpression`.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.CollectionExpression`
            Expression of the same type as the callee.
        """
        # FIXME: enable doctest

        def unify_ret(t):
            if t != tbool:
                raise TypeError("'filter' expects 'f' to return an expression of type 'bool', found '{}'".format(t))
            return hl.tarray(self._type.element_type)

        def transform_ir(array, name, body):
            return ArrayFilter(array, name, body)

        array_filter = hl.array(self)._ir_lambda_method(transform_ir, f, self.dtype.element_type, unify_ret)

        if isinstance(self.dtype, tset):
            return hl.set(array_filter)
        else:
            assert isinstance(self.dtype, tarray), self.dtype
            return array_filter

    @typecheck_method(f=func_spec(1, expr_bool))
    def find(self, f):
        """Returns the first element where `f` returns ``True``.

        Examples
        --------

        >>> hl.eval(a.find(lambda x: x ** 2 > 20))
        5

        >>> hl.eval(s3.find(lambda x: x[0] == 'D'))
        None

        Notes
        -----
        If `f` returns ``False`` for every element, then the result is missing.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.Expression`
            Expression whose type is the element type of the collection.
        """

        # FIXME this should short-circuit
        return self.fold(lambda accum, x:
                         hl.cond(hl.is_missing(accum) & f(x), x, accum),
                         hl.null(self._type.element_type))

    @typecheck_method(f=func_spec(1, expr_any))
    def flatmap(self, f):
        """Map each element of the collection to a new collection, and flatten the results.

        Examples
        --------

        >>> hl.eval(a.flatmap(lambda x: hl.range(0, x)))
        [0, 0, 1, 0, 1, 2, 0, 1, 2, 3, 0, 1, 2, 3, 4]

        >>> hl.eval(s3.flatmap(lambda x: hl.set(hl.range(0, x.length()).map(lambda i: x[i]))))  # doctest: +SKIP_OUTPUT_CHECK
        {'A', 'B', 'C', 'a', 'b', 'c', 'e', 'h', 'i', 'l', 'o', 'r'}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.CollectionExpression`)
            Function from the element type of the collection to the type of the
            collection. For instance, `flatmap` on a ``set<str>`` should take
            a ``str`` and return a ``set``.

        Returns
        -------
        :class:`.CollectionExpression`
        """
        expected_type, s = (tarray, 'array') if isinstance(self._type, tarray) else (tset, 'set')
        value_type = f(construct_variable(Env.get_uid(), self.dtype.element_type)).dtype

        if not isinstance(value_type, expected_type):
            raise TypeError("'flatmap' expects 'f' to return an expression of type '{}', found '{}'".format(s, value_type))

        def f2(x):
            return hl.array(f(x)) if isinstance(value_type, tset) else f(x)

        def transform_ir(array, name, body):
            return ArrayFlatMap(array, name, body)

        array_flatmap = hl.array(self)._ir_lambda_method(transform_ir, f2, self.dtype.element_type, identity)

        if isinstance(self.dtype, tset):
            return hl.set(array_flatmap)
        assert isinstance(self.dtype, tarray), self.dtype
        return array_flatmap

    @typecheck_method(f=func_spec(2, expr_any), zero=expr_any)
    def fold(self, f, zero):
        """Reduces the collection with the given function `f`, provided the initial value `zero`.

        Examples
        --------
        >>> a = [0, 1, 2]

        >>> hl.eval(hl.fold(lambda i, j: i + j, 0, a))
        3

        Parameters
        ----------
        f : function ( (:class:`.Expression`, :class:`.Expression`) -> :class:`.Expression`)
            Function which takes the cumulative value and the next element, and
            returns a new value.
        zero : :class:`.Expression`
            Initial value to pass in as left argument of `f`.

        Returns
        -------
        :class:`.Expression`.
        """
        collection = self
        if isinstance(collection.dtype, tset):
            collection = hl.array(collection)
        indices, aggregations = unify_all(collection, zero)
        accum_name = Env.get_uid()
        elt_name = Env.get_uid()

        accum_ref = construct_variable(accum_name, zero.dtype, indices, aggregations)
        elt_ref = construct_variable(elt_name, collection.dtype.element_type, collection._indices, collection._aggregations)
        body = to_expr(f(accum_ref, elt_ref))

        if body.dtype != zero.dtype:
            zero_coercer = coercer_from_dtype(zero.dtype)
            if zero_coercer.can_coerce(body.dtype):
                body = zero_coercer.coerce(body)
            else:
                body_coercer = coercer_from_dtype(body.dtype)
                if body_coercer.can_coerce(zero.dtype):
                    zero_coerced = body_coercer.coerce(zero)
                    accum_ref = construct_variable(accum_name, zero_coerced.dtype, indices, aggregations)
                    new_body = to_expr(f(accum_ref, elt_ref))
                    if body_coercer.can_coerce(new_body.dtype):
                        body = body_coercer.coerce(new_body)
                        zero = zero_coerced

        if body.dtype != zero.dtype:
            raise ExpressionException("'CollectionExpression.fold' must take function returning "
                                      "same expression type as zero value: \n"
                                      "    zero.dtype: {}\n"
                                      "    f.dtype: {}".format(
                zero.dtype,
                body.dtype))

        ir = ArrayFold(collection._ir, zero._ir, accum_name, elt_name, body._ir)

        indices, aggregations = unify_all(self, zero, body)
        return construct_expr(ir, body.dtype, indices, aggregations)


    @typecheck_method(f=func_spec(1, expr_bool))
    def all(self, f):
        """Returns ``True`` if `f` returns ``True`` for every element.

        Examples
        --------

        >>> hl.eval(a.all(lambda x: x < 10))
        True

        Notes
        -----
        This method returns ``True`` if the collection is empty.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.BooleanExpression`)
            Function to evaluate for each element of the collection. Must return a
            :class:`.BooleanExpression`.

        Returns
        -------
        :class:`.BooleanExpression`.
            ``True`` if `f` returns ``True`` for every element, ``False`` otherwise.
        """
        f2 = lambda accum, elt: accum & f(elt)
        return hl.array(self).fold(f2, True)

    @typecheck_method(f=func_spec(1, expr_any))
    def group_by(self, f):
        """Group elements into a dict according to a lambda function.

        Examples
        --------

        >>> hl.eval(a.group_by(lambda x: x % 2 == 0))  # doctest: +SKIP_OUTPUT_CHECK
        {False: [1, 3, 5], True: [2, 4]}

        >>> hl.eval(s3.group_by(lambda x: x.length()))  # doctest: +SKIP_OUTPUT_CHECK
        {3: {'Bob'}, 5: {'Alice'}, 7: {'Charlie'}}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to evaluate for each element of the collection to produce a key for the
            resulting dictionary.

        Returns
        -------
        :class:`.DictExpression`.
            Dictionary keyed by results of `f`.
        """

        keyed = hl.array(self).map(lambda x: hl.tuple([f(x), x]))
        types = keyed.dtype.element_type.types
        return construct_expr(GroupByKey(keyed._ir), tdict(types[0], tarray(types[1])), keyed._indices, keyed._aggregations)

    @typecheck_method(f=func_spec(1, expr_any))
    def map(self, f):
        """Transform each element of a collection.

        Examples
        --------

        >>> hl.eval(a.map(lambda x: x ** 3))
        [1.0, 8.0, 27.0, 64.0, 125.0]

        >>> hl.eval(s3.map(lambda x: x.length()))
        {3, 5, 7}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to transform each element of the collection.

        Returns
        -------
        :class:`.CollectionExpression`.
            Collection where each element has been transformed according to `f`.
        """

        def transform_ir(array, name, body):
            a = ArrayMap(array, name, body)
            if isinstance(self.dtype, tset):
                a = ToSet(a)
            return a

        array_map = hl.array(self)._ir_lambda_method(transform_ir, f, self._type.element_type, lambda t: self._type.__class__(t))

        if isinstance(self._type, tset):
            return hl.set(array_map)
        assert isinstance(self._type, tarray)
        return array_map

    def length(self):
        """Returns the size of a collection.

        Examples
        --------

        >>> hl.eval(a.length())
        5

        >>> hl.eval(s3.length())
        3

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            The number of elements in the collection.
        """
        return apply_expr(lambda x: ArrayLen(x), tint32, hl.array(self))

    def size(self):
        """Returns the size of a collection.

        Examples
        --------
        
        >>> hl.eval(a.size())
        5

        >>> hl.eval(s3.size())
        3

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            The number of elements in the collection.
        """
        return apply_expr(lambda x: ArrayLen(x), tint32, hl.array(self))

    def _extra_summary_fields(self, agg_result):
        return {
            'Min Size': agg_result[0],
            'Max Size': agg_result[1],
            'Mean Size': agg_result[2],
        }

    def _nested_summary(self, agg_result, top):
        elt = construct_variable(Env.get_uid(), self.dtype.element_type, indices=self._indices)
        return {'[<elements>]': elt._summarize(agg_result[3])}

    def _summary_aggs(self):
        length = hl.len(self)
        return hl.tuple((
            hl.agg.min(length),
            hl.agg.max(length),
            hl.agg.mean(length),
            hl.agg.explode(lambda elt: elt._all_summary_aggs(), self)))


class ArrayExpression(CollectionExpression):
    """Expression of type :class:`.tarray`.

    >>> names = hl.literal(['Alice', 'Bob', 'Charlie'])

    See Also
    --------
    :class:`.CollectionExpression`
    """

    def __getitem__(self, item):
        """Index into or slice the array.

        Examples
        --------

        Index with a single integer:

        >>> hl.eval(names[1])
        'Bob'

        >>> hl.eval(names[-1])
        'Charlie'

        Slicing is also supported:

        >>> hl.eval(names[1:])
        ['Bob', 'Charlie']

        Parameters
        ----------
        item : slice or :class:`.Expression` of type :py:data:`.tint32`
            Index or slice.

        Returns
        -------
        :class:`.Expression`
            Element or array slice.
        """
        if isinstance(item, slice):
            return self._slice(self.dtype, item.start, item.stop, item.step)
        elif isinstance(item, str):
            return CollectionExpression.__getitem__(self, item)
        item = to_expr(item)
        if not item.dtype == tint32:
            raise TypeError("array expects key to be type 'slice' or expression of type 'int32', "
                            "found expression of type '{}'".format(item._type))
        else:
            return self._method("indexArray", self.dtype.element_type, item)

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns a boolean indicating whether `item` is found in the array.

        Examples
        --------

        >>> hl.eval(names.contains('Charlie'))
        True

        >>> hl.eval(names.contains('Helen'))
        False

        Parameters
        ----------
        item : :class:`.Expression`
            Item for inclusion test.

        Warning
        -------
        This method takes time proportional to the length of the array. If a
        pipeline uses this method on the same array several times, it may be
        more efficient to convert the array to a set first early in the script
        (:func:`~hail.expr.functions.set`).

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the element is found in the array, ``False`` otherwise.
        """
        return self._method("contains", tbool, item)

    def head(self):
        """Returns the first element of the array, or missing if empty.

        Returns
        -------
        :class:`.Expression`
            Element.

        Examples
        --------
        >>> hl.eval(names.head())
        'Alice'

        If the array has no elements, then the result is missing:
        >>> hl.eval(names.filter(lambda x: x.startswith('D')).head())
        None
        """
        # FIXME: this should generate short-circuiting IR when that is possible
        return hl.rbind(self, lambda x: hl.case().when(x.length() > 0, x[0]).or_missing())

    @typecheck_method(x=oneof(func_spec(1, expr_any), expr_any))
    def index(self, x):
        """Returns the first index of `x`, or missing.

        Parameters
        ----------
        x : :class:`.Expression` or :obj:`Callable`
            Value to find, or function from element to Boolean expression.

        Returns
        -------
        :class:`.Int32Expression`

        Examples
        --------
        >>> hl.eval(names.index('Bob'))
        1

        >>> hl.eval(names.index('Beth'))
        None

        >>> hl.eval(names.index(lambda x: x.endswith('e')))
        0

        >>> hl.eval(names.index(lambda x: x.endswith('h')))
        None
        """
        if callable(x):
            f = lambda elt, x: x(elt)
        else:
            f = lambda elt, x: elt == x
        return hl.bind(lambda a: hl.range(0, a.length()).filter(lambda i: f(a[i], x)).head(), self)

    @typecheck_method(item=expr_any)
    def append(self, item):
        """Append an element to the array and return the result.

        Examples
        --------

        >>> hl.eval(names.append('Dan'))
        ['Alice', 'Bob', 'Charlie', 'Dan']

        Note
        ----
        This method does not mutate the caller, but instead returns a new
        array by copying the caller and adding `item`.

        Parameters
        ----------
        item : :class:`.Expression`
            Element to append, same type as the array element type.

        Returns
        -------
        :class:`.ArrayExpression`
        """
        if not item._type == self._type.element_type:
            raise TypeError("'ArrayExpression.append' expects 'item' to be the same type as its elements\n"
                            "    array element type: '{}'\n"
                            "    type of arg 'item': '{}'".format(self._type._element_type, item._type))
        return self._method("append", self._type, item)

    @typecheck_method(a=expr_array())
    def extend(self, a):
        """Concatenate two arrays and return the result.

        Examples
        --------

        >>> hl.eval(names.extend(['Dan', 'Edith']))
        ['Alice', 'Bob', 'Charlie', 'Dan', 'Edith']

        Parameters
        ----------
        a : :class:`.ArrayExpression`
            Array to concatenate, same type as the callee.

        Returns
        -------
        :class:`.ArrayExpression`
        """
        if not a._type == self._type:
            raise TypeError("'ArrayExpression.extend' expects 'a' to be the same type as the caller\n"
                            "    caller type: '{}'\n"
                            "    type of 'a': '{}'".format(self._type, a._type))
        return self._method("extend", self._type, a)

    @typecheck_method(f=func_spec(2, expr_any), zero=expr_any)
    def scan(self, f, zero):
        """Map each element of the array to cumulative value of function `f`, with initial value `zero`.

        Examples
        --------
        >>> a = [0, 1, 2]

        >>> hl.eval(hl.array_scan(lambda i, j: i + j, 0, a))
        [0, 0, 1, 3]

        Parameters
        ----------
        f : function ( (:class:`.Expression`, :class:`.Expression`) -> :class:`.Expression`)
            Function which takes the cumulative value and the next element, and
            returns a new value.
        zero : :class:`.Expression`
            Initial value to pass in as left argument of `f`.

        Returns
        -------
        :class:`.ArrayExpression`.
        """
        indices, aggregations = unify_all(self, zero)
        accum_name = Env.get_uid()
        elt_name = Env.get_uid()

        accum_ref = construct_variable(accum_name, zero.dtype, indices, aggregations)
        elt_ref = construct_variable(elt_name, self.dtype.element_type, self._indices, self._aggregations)
        body = to_expr(f(accum_ref, elt_ref))

        if body.dtype != zero.dtype:
            zero_coercer = coercer_from_dtype(zero.dtype)
            if zero_coercer.can_coerce(body.dtype):
                body = zero_coercer.coerce(body)
            else:
                body_coercer = coercer_from_dtype(body.dtype)
                if body_coercer.can_coerce(zero.dtype):
                    zero_coerced = body_coercer.coerce(zero)
                    accum_ref = construct_variable(accum_name, zero_coerced.dtype, indices, aggregations)
                    new_body = to_expr(f(accum_ref, elt_ref))
                    if body_coercer.can_coerce(new_body.dtype):
                        body = body_coercer.coerce(new_body)
                        zero = zero_coerced

        if body.dtype != zero.dtype:
            raise ExpressionException("'ArrayExpression.scan' must take function returning "
                                      "same expression type as zero value: \n"
                                      "    zero.dtype: {}\n"
                                      "    f.dtype: {}".format(
                zero.dtype,
                body.dtype))

        ir = ArrayScan(self._ir, zero._ir, accum_name, elt_name, body._ir)

        indices, aggregations = unify_all(self, zero, body)
        return construct_expr(ir, tarray(body.dtype), indices, aggregations)


class ArrayStructExpression(ArrayExpression):
    """Expression of type :class:`.tarray` that eventually contains structs.

    >>> people = hl.literal([hl.struct(name='Alice', age=57),
    ...                      hl.struct(name='Bob', age=12),
    ...                      hl.struct(name='Charlie', age=34)])

    Nested collections that contain structs are also
    :class:`.ArrayStructExpressions`s

    >>> people = hl.literal([[hl.struct(name='Alice', age=57), hl.struct(name='Bob', age=12)],
    ...                      [hl.struct(name='Charlie', age=34)]])

    See Also
    --------
    :class:`.ArrayExpression`, class:`.CollectionExpression`, :class:`.SetStructExpression`
    """

    def __getattr__(self, item):
        return ArrayStructExpression.__getitem__(self, item)

    def __getitem__(self, item):
        """If a string, get a field from each struct in this array. If an integer, get
        the item at that index.

        Examples
        --------

        >>> x = hl.array([hl.struct(a='foo', b=3), hl.struct(a='bar', b=4)])
        >>> hl.eval(x.a)
        ['foo', 'bar']

        >>> a = hl.array([hl.struct(b=[hl.struct(inner=1),
        ...                            hl.struct(inner=2)]),
        ...               hl.struct(b=[hl.struct(inner=3)])])
        >>> hl.eval(a.b)
        [[Struct(inner=1), Struct(inner=2)], [Struct(inner=3)]]
        >>> hl.eval(a.b.inner)
        [[1, 2], [3]]
        >>> hl.eval(hl.flatten(a.b).inner)
        [1, 2, 3]
        >>> hl.eval(hl.flatten(a.b.inner))
        [1, 2, 3]

        Parameters
        ----------
        item : :obj:`str`
            Field name

        Returns
        -------
        :class:`.ArrayExpression`
            An array formed by getting the given field for each struct in
            this array

        See Also
        --------
        :meth:`.ArrayExpression.__getitem__`
        """

        if isinstance(item, str):
            return self.map(lambda x: x[item])
        return super().__getitem__(item)


class ArrayNumericExpression(ArrayExpression):
    """Expression of type :class:`.tarray` with a numeric type.

    Numeric arrays support arithmetic both with scalar values and other arrays.
    Arithmetic between two numeric arrays requires that the length of each array
    is identical, and will apply the operation positionally (``a1 * a2`` will
    multiply the first element of ``a1`` by the first element of ``a2``, the
    second element of ``a1`` by the second element of ``a2``, and so on).
    Arithmetic with a scalar will apply the operation to each element of the
    array.

    >>> a1 = hl.literal([0, 1, 2, 3, 4, 5])

    >>> a2 = hl.literal([1, -1, 1, -1, 1, -1])

    """

    def __neg__(self):
        """Negate elements of the array.

        Examples
        --------

        >>> hl.eval(-a1)
        [0, -1, -2, -3, -4, -5]

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array expression of the same type.
        """
        return self * -1

    def __add__(self, other):
        """Positionally add an array or a scalar.

        Examples
        --------

        >>> hl.eval(a1 + 5)
        [5, 6, 7, 8, 9, 10]

        >>> hl.eval(a1 + a2)
        [1, 0, 3, 2, 5, 4]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to add.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional sums.
        """
        return self._bin_op_numeric("+", other)

    def __radd__(self, other):
        return self._bin_op_numeric_reverse("+", other)

    def __sub__(self, other):
        """Positionally subtract an array or a scalar.

        Examples
        --------

        >>> hl.eval(a2 - 1)
        [0, -2, 0, -2, 0, -2]

        >>> hl.eval(a1 - a2)
        [-1, 2, 1, 4, 3, 6]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to subtract.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional differences.
        """
        return self._bin_op_numeric("-", other)

    def __rsub__(self, other):
        return self._bin_op_numeric_reverse("-", other)

    def __mul__(self, other):
        """Positionally multiply by an array or a scalar.

        Examples
        --------

        >>> hl.eval(a2 * 5)
        [5, -5, 5, -5, 5, -5]

        >>> hl.eval(a1 * a2)
        [0, -1, 2, -3, 4, -5]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to multiply by.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional products.
        """
        return self._bin_op_numeric("*", other)

    def __rmul__(self, other):
        return self._bin_op_numeric_reverse("*", other)

    def __truediv__(self, other):
        """Positionally divide by an array or a scalar.

        Examples
        --------

        >>> hl.eval(a1 / 10)  # doctest: +SKIP_OUTPUT_CHECK
        [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]

        >>> hl.eval(a2 / a1)  # doctest: +SKIP_OUTPUT_CHECK
        [inf, -1.0, 0.5, -0.3333333333333333, 0.25, -0.2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`
            Value or array to divide by.

        Returns
        -------
        :class:`.ArrayNumericExpression`
            Array of positional quotients.
        """
        return self._bin_op_numeric("/", other, self._div_ret_type_f)

    def __rtruediv__(self, other):
        return self._bin_op_numeric_reverse("/", other, self._div_ret_type_f)

    def __floordiv__(self, other):
        """Positionally divide by an array or a scalar using floor division.

        Examples
        --------

        >>> hl.eval(a1 // 2)
        [0, 0, 1, 1, 2, 2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('//', other)

    def __rfloordiv__(self, other):
        return self._bin_op_numeric_reverse('//', other)

    def __mod__(self, other):
        """Positionally compute the left modulo the right.

        Examples
        --------

        >>> hl.eval(a1 % 2)
        [0, 1, 0, 1, 0, 1]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('%', other)

    def __rmod__(self, other):
        return self._bin_op_numeric_reverse('%', other)

    def __pow__(self, other):
        """Positionally raise to the power of an array or a scalar.

        Examples
        --------

        >>> hl.eval(a1 ** 2)
        [0.0, 1.0, 4.0, 9.0, 16.0, 25.0]

        >>> hl.eval(a1 ** a2)
        [0.0, 1.0, 2.0, 0.3333333333333333, 4.0, 0.2]

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.ArrayNumericExpression`

        Returns
        -------
        :class:`.ArrayNumericExpression`
        """
        return self._bin_op_numeric('**', other, lambda _: tfloat64)

    def __rpow__(self, other):
        return self._bin_op_numeric_reverse('**', other, lambda _: tfloat64)


class SetExpression(CollectionExpression):
    """Expression of type :class:`.tset`.

    >>> s1 = hl.literal({1, 2, 3})
    >>> s2 = hl.literal({1, 3, 5})

    See Also
    --------
    :class:`.CollectionExpression`
    """

    @typecheck_method(ir=IR, type=HailType, indices=Indices, aggregations=LinkedList)
    def __init__(self, ir, type, indices=Indices(), aggregations=LinkedList(Aggregation)):
        super(SetExpression, self).__init__(ir, type, indices, aggregations)
        assert isinstance(type, tset)
        self._ec = coercer_from_dtype(type.element_type)

    @typecheck_method(item=expr_any)
    def add(self, item):
        """Returns a new set including `item`.

        Examples
        --------

        >>> hl.eval(s1.add(10))  # doctest: +SKIP_OUTPUT_CHECK
        {1, 2, 3, 10}

        Parameters
        ----------
        item : :class:`.Expression`
            Value to add.

        Returns
        -------
        :class:`.SetExpression`
            Set with `item` added.
        """
        if not self._ec.can_coerce(item.dtype):
            raise TypeError("'SetExpression.add' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self.dtype.element_type, item.dtype))
        return self._method("add", self.dtype, self._ec.coerce(item))

    @typecheck_method(item=expr_any)
    def remove(self, item):
        """Returns a new set excluding `item`.

        Examples
        --------

        >>> hl.eval(s1.remove(1))
        {2, 3}

        Parameters
        ----------
        item : :class:`.Expression`
            Value to remove.

        Returns
        -------
        :class:`.SetExpression`
            Set with `item` removed.
        """
        if not self._ec.can_coerce(item.dtype):
            raise TypeError("'SetExpression.remove' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self.dtype.element_type, item.dtype))
        return self._method("remove", self._type, self._ec.coerce(item))

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns ``True`` if `item` is in the set.

        Examples
        --------

        >>> hl.eval(s1.contains(1))
        True

        >>> hl.eval(s1.contains(10))
        False

        Parameters
        ----------
        item : :class:`.Expression`
            Value for inclusion test.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `item` is in the set.
        """
        if not self._ec.can_coerce(item.dtype):
            raise TypeError("'SetExpression.contains' expects 'item' to be the same type as its elements\n"
                            "    set element type:   '{}'\n"
                            "    type of arg 'item': '{}'".format(self.dtype.element_type, item.dtype))
        return self._method("contains", tbool, self._ec.coerce(item))

    @typecheck_method(s=expr_set())
    def difference(self, s):
        """Return the set of elements in the set that are not present in set `s`.

        Examples
        --------

        >>> hl.eval(s1.difference(s2))
        {2}

        >>> hl.eval(s2.difference(s1))
        {5}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements not in `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.difference' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("difference", self._type, s)

    @typecheck_method(s=expr_set())
    def intersection(self, s):
        """Return the intersection of the set and set `s`.

        Examples
        --------
        
        >>> hl.eval(s1.intersection(s2))
        {1, 3}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements present in `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.intersection' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("intersection", self._type, s)

    @typecheck_method(s=expr_set())
    def is_subset(self, s):
        """Returns ``True`` if every element is contained in set `s`.

        Examples
        --------

        >>> hl.eval(s1.is_subset(s2))
        False

        >>> hl.eval(s1.remove(2).is_subset(s2))
        True

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if every element is contained in set `s`.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.is_subset' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("isSubset", tbool, s)

    @typecheck_method(s=expr_set())
    def union(self, s):
        """Return the union of the set and set `s`.

        Examples
        --------

        >>> hl.eval(s1.union(s2))
        {1, 2, 3, 5}

        Parameters
        ----------
        s : :class:`.SetExpression`
            Set expression of the same type.

        Returns
        -------
        :class:`.SetExpression`
            Set of elements present in either set.
        """
        if not s._type.element_type == self._type.element_type:
            raise TypeError("'SetExpression.union' expects 's' to be the same type\n"
                            "    set type:    '{}'\n"
                            "    type of 's': '{}'".format(self._type, s._type))
        return self._method("union", self._type, s)


class SetStructExpression(SetExpression):
    """Expression of type :class:`.tset` that eventually contains structs.

    >>> people = hl.literal({hl.struct(name='Alice', age=57),
    ...                      hl.struct(name='Bob', age=12),
    ...                      hl.struct(name='Charlie', age=34)})

    Nested collections that contain structs are also
    :class:`.SetStructExpressions`s

    >>> people = hl.set([hl.set([hl.struct(name='Alice', age=57), hl.struct(name='Bob', age=12)]),
    ...                  hl.set([hl.struct(name='Charlie', age=34)])])

    See Also
    --------
    :class:`.SetExpression`, class:`.CollectionExpression`, :class:`.SetStructExpression`
    """

    def __getattr__(self, item):
        return SetStructExpression.__getitem__(self, item)

    @typecheck_method(item=oneof(str))
    def __getitem__(self, item):
        """Get a field from each struct in this set.

        Examples
        --------

        >>> x = hl.set({hl.struct(a='foo', b=3), hl.struct(a='bar', b=4)})
        >>> hl.eval(x.a) == {'foo', 'bar'}
        True

        >>> a = hl.set({hl.struct(b={hl.struct(inner=1),
        ...                          hl.struct(inner=2)}),
        ...             hl.struct(b={hl.struct(inner=3)})})
        >>> hl.eval(hl.flatten(a.b).inner) == {1, 2, 3}
        True
        >>> hl.eval(hl.flatten(a.b.inner)) == {1, 2, 3}
        True

        Parameters
        ----------
        item : :obj:`str`
            Field name

        Returns
        -------
        :class:`.SetExpression`
            A set formed by getting the given field for each struct in
            this set
        """

        return self.map(lambda x: x[item])


class DictExpression(Expression):
    """Expression of type :class:`.tdict`.

    >>> d = hl.literal({'Alice': 43, 'Bob': 33, 'Charles': 44})
    """

    @typecheck_method(ir=IR, type=HailType, indices=Indices, aggregations=LinkedList)
    def __init__(self, ir, type, indices=Indices(), aggregations=LinkedList(Aggregation)):
        super(DictExpression, self).__init__(ir, type, indices, aggregations)
        assert isinstance(type, tdict)
        self._kc = coercer_from_dtype(type.key_type)
        self._vc = coercer_from_dtype(type.value_type)

    @typecheck_method(item=expr_any)
    def __getitem__(self, item):
        """Get the value associated with key `item`.

        Examples
        --------

        >>> hl.eval(d['Alice'])
        43

        Notes
        -----
        Raises an error if `item` is not a key of the dictionary. Use
        :meth:`.DictExpression.get` to return missing instead of an error.

        Parameters
        ----------
        item : :class:`.Expression`
            Key expression.

        Returns
        -------
        :class:`.Expression`
            Value associated with key `item`.
        """
        if not self._kc.can_coerce(item.dtype):
            raise TypeError("dict encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self.dtype.key_type, item.dtype))
        return self._index(self.dtype.value_type, self._kc.coerce(item))

    @typecheck_method(item=expr_any)
    def contains(self, item):
        """Returns whether a given key is present in the dictionary.

        Examples
        --------

        >>> hl.eval(d.contains('Alice'))
        True

        >>> hl.eval(d.contains('Anne'))
        False

        Parameters
        ----------
        item : :class:`.Expression`
            Key to test for inclusion.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `item` is a key of the dictionary, ``False`` otherwise.
        """
        if not self._kc.can_coerce(item.dtype):
            raise TypeError("'DictExpression.contains' encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self._type.key_type, item.dtype))
        return self._method("contains", tbool, self._kc.coerce(item))

    @typecheck_method(item=expr_any, default=nullable(expr_any))
    def get(self, item, default=None):
        """Returns the value associated with key `k` or a default value if that key is not present.

        Examples
        --------

        >>> hl.eval(d.get('Alice'))
        43

        >>> hl.eval(d.get('Anne'))
        None

        >>> hl.eval(d.get('Anne', 0))
        0

        Parameters
        ----------
        item : :class:`.Expression`
            Key.
        default : :class:`.Expression`
            Default value. Must be same type as dictionary values.

        Returns
        -------
        :class:`.Expression`
            The value associated with `item`, or `default`.
        """
        if not self._kc.can_coerce(item.dtype):
            raise TypeError("'DictExpression.get' encountered an invalid key type\n"
                            "    dict key type:  '{}'\n"
                            "    type of 'item': '{}'".format(self.dtype.key_type, item.dtype))
        key = self._kc.coerce(item)

        if default is not None:
            if not self._vc.can_coerce(default.dtype):
                raise TypeError("'get' expects parameter 'default' to have the same type "
                                "as the dictionary value type, expected '{}' and found '{}'"
                                .format(self.dtype.value_type, default.dtype))
            return self._method("get", self.dtype.value_type, key, self._vc.coerce(default))
        else:
            return self._method("get", self.dtype.value_type, key)

    def key_set(self):
        """Returns the set of keys in the dictionary.

        Examples
        --------

        >>> hl.eval(d.key_set())  # doctest: +SKIP_OUTPUT_CHECK
        {'Alice', 'Bob', 'Charles'}

        Returns
        -------
        :class:`.SetExpression`
            Set of all keys.
        """
        return self._method("keySet", tset(self.dtype.key_type))

    def keys(self):
        """Returns an array with all keys in the dictionary.

        Examples
        --------

        >>> hl.eval(d.keys())  # doctest: +SKIP_OUTPUT_CHECK
        ['Bob', 'Charles', 'Alice']

        Returns
        -------
        :class:`.ArrayExpression`
            Array of all keys.
        """
        return self._method("keys", tarray(self.dtype.key_type))

    @typecheck_method(f=func_spec(1, expr_any))
    def map_values(self, f):
        """Transform values of the dictionary according to a function.

        Examples
        --------

        >>> hl.eval(d.map_values(lambda x: x * 10))  # doctest: +SKIP_OUTPUT_CHECK
        {'Alice': 430, 'Bob': 330, 'Charles': 440}

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to apply to each value.

        Returns
        -------
        :class:`.DictExpression`
            Dictionary with transformed values.
        """
        return hl.dict(hl.array(self).map(lambda elt: hl.tuple([elt[0], f(elt[1])])))

    def size(self):
        """Returns the size of the dictionary.

        Examples
        --------

        >>> hl.eval(d.size())
        3

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            Size of the dictionary.
        """
        return apply_expr(lambda x: ArrayLen(ToArray(x)), tint32, self)

    def values(self):
        """Returns an array with all values in the dictionary.

        Examples
        --------

        >>> hl.eval(d.values())  # doctest: +SKIP_OUTPUT_CHECK
        [33, 44, 43]

        Returns
        -------
        :class:`.ArrayExpression`
            All values in the dictionary.
        """
        return self._method("values", tarray(self.dtype.value_type))

    def _extra_summary_fields(self, agg_result):
        return {
            'Min Size': agg_result[0],
            'Max Size': agg_result[1],
            'Mean Size': agg_result[2],
        }

    def _nested_summary(self, agg_result, top):
        k = construct_variable(Env.get_uid(), self.dtype.key_type, indices=self._indices)
        v = construct_variable(Env.get_uid(), self.dtype.value_type, indices=self._indices)
        return {
            '[<keys>]': k._summarize(agg_result[3][0]),
            '[<values>]': v._summarize(agg_result[3][1]),

        }

    def _summary_aggs(self):
        length = hl.len(self)
        return hl.tuple((
            hl.agg.min(length),
            hl.agg.max(length),
            hl.agg.mean(length),
            hl.agg.explode(lambda elt: hl.tuple((elt[0]._all_summary_aggs(), elt[1]._all_summary_aggs())), hl.array(self))))


class StructExpression(Mapping[str, Expression], Expression):
    """Expression of type :class:`.tstruct`.

    >>> struct = hl.struct(a=5, b='Foo')

    Struct fields are accessible as attributes and keys. It is therefore
    possible to access field `a` of struct `s` with dot syntax:

    >>> hl.eval(struct.a)
    5

    However, it is recommended to use square brackets to select fields:

    >>> hl.eval(struct['a'])
    5

    The latter syntax is safer, because fields that share their name with
    an existing attribute of :class:`.StructExpression` (`keys`, `values`,
    `annotate`, `drop`, etc.) will only be accessible using the
    :meth:`.StructExpression.__getitem__` syntax. This is also the only way
    to access fields that are not valid Python identifiers, like fields with
    spaces or symbols.
    """

    @classmethod
    def _from_fields(cls, fields: 'Dict[str, Expression]'):
        t = tstruct(**{k: v.dtype for k, v in fields.items()})
        ir = MakeStruct([(n, expr._ir) for (n, expr) in fields.items()])
        indices, aggregations = unify_all(*fields.values())
        s = StructExpression.__new__(cls)
        s._fields = {}
        for k, v in fields.items():
            s._set_field(k, v)
        super(StructExpression, s).__init__(ir, t, indices, aggregations)
        return s

    @typecheck_method(ir=IR, type=HailType, indices=Indices, aggregations=LinkedList)
    def __init__(self, ir, type, indices=Indices(), aggregations=LinkedList(Aggregation)):
        super(StructExpression, self).__init__(ir, type, indices, aggregations)
        self._fields: Dict[str, Expression] = {}

        for i, (f, t) in enumerate(self.dtype.items()):
            if isinstance(self._ir, MakeStruct):
                expr = construct_expr(self._ir.fields[i][1], t, self._indices,
                                          self._aggregations)
            elif isinstance(self._ir, SelectFields):
                expr = construct_expr(GetField(self._ir.old, f), t, self._indices,
                                      self._aggregations)
            else:
                expr = construct_expr(GetField(self._ir, f), t, self._indices,
                                          self._aggregations)
            self._set_field(f, expr)

    def _set_field(self, key, value):
        self._fields[key] = value
        if key not in self.__dict__:
            self.__dict__[key] = value

    def _get_field(self, item):
        if item in self._fields:
            return self._fields[item]
        else:
            raise KeyError(get_nice_field_error(self, item))

    def __getattr__(self, item):
        if item in self.__dict__:
            return self.__dict__[item]
        else:
            raise AttributeError(get_nice_attr_error(self, item))

    def __len__(self):
        return len(self._fields)

    @typecheck_method(item=oneof(str, int, slice))
    def __getitem__(self, item):
        """Access a field of the struct by name or index.

        Examples
        --------

        >>> hl.eval(struct['a'])
        5

        >>> hl.eval(struct[1])
        'Foo'

        Parameters
        ----------
        item : :obj:`str`
            Field name.

        Returns
        -------
        :class:`.Expression`
            Struct field.
        """
        if isinstance(item, str):
            return self._get_field(item)
        if isinstance(item, int):
            return self._get_field(self.dtype.fields[item])
        else:
            assert item.start is None or isinstance(item.start, int)
            assert item.stop is None or isinstance(item.stop, int)
            assert item.step is None or isinstance(item.step, int)
            return self.select(
                *self.dtype.fields[item.start:item.stop:item.step])

    def __iter__(self):
        return iter(self._fields)

    def __hash__(self):
        return object.__hash__(self)

    def __eq__(self, other):
        return Expression.__eq__(self, other)

    def __ne__(self, other):
        return Expression.__ne__(self, other)

    def __nonzero__(self):
        return Expression.__nonzero__(self)

    def _annotate_ordered(self, insertions_dict, field_order):
        def get_type(field):
            e = insertions_dict.get(field)
            if e is None:
                e = self._fields[field]
            return e.dtype

        new_type = hl.tstruct(**{f: get_type(f) for f in field_order})
        indices, aggregations = unify_all(self, *insertions_dict.values())
        return construct_expr(InsertFields.construct_with_deduplication(
            self._ir, [(field, expr._ir) for field, expr in insertions_dict.items()], field_order),
            new_type,
            indices,
            aggregations)

    @typecheck_method(named_exprs=expr_any)
    def annotate(self, **named_exprs):
        """Add new fields or recompute existing fields.

        Examples
        --------

        >>> hl.eval(struct.annotate(a=10, c=2*2*2))
        Struct(a=10, b='Foo', c=8)

        Notes
        -----
        If an expression in `named_exprs` shares a name with a field of the
        struct, then that field will be replaced but keep its position in
        the struct. New fields will be appended to the end of the struct.

        Parameters
        ----------
        named_exprs : keyword args of :class:`.Expression`
            Fields to add.

        Returns
        -------
        :class:`.StructExpression`
            Struct with new or updated fields.
        """
        new_types = {n: t for (n, t) in self.dtype.items()}

        for f, e in named_exprs.items():
            new_types[f] = e.dtype

        result_type = tstruct(**new_types)
        indices, aggregations = unify_all(self, *[x for (f, x) in named_exprs.items()])

        return construct_expr(InsertFields.construct_with_deduplication(
            self._ir, list(map(lambda x: (x[0], x[1]._ir), named_exprs.items())), None),
            result_type, indices, aggregations)

    @typecheck_method(fields=str, named_exprs=expr_any)
    def select(self, *fields, **named_exprs):
        """Select existing fields and compute new ones.

        Examples
        --------

        >>> hl.eval(struct.select('a', c=['bar', 'baz']))
        Struct(a=5, c=['bar', 'baz'])

        Notes
        -----
        The `fields` argument is a list of field names to keep. These fields
        will appear in the resulting struct in the order they appear in
        `fields`.

        The `named_exprs` arguments are new field expressions.

        Parameters
        ----------
        fields : varargs of :obj:`str`
            Field names to keep.
        named_exprs : keyword args of :class:`.Expression`
            New field expressions.

        Returns
        -------
        :class:`.StructExpression`
            Struct containing specified existing fields and computed fields.
        """

        name_set = set()
        for a in fields:
            if not a in self._fields:
                raise KeyError("Struct has no field '{}'\n"
                               "    Fields: [ {} ]".format(a, ', '.join("'{}'".format(x) for x in self._fields)))
            if a in name_set:
                raise ExpressionException("'StructExpression.select' does not support duplicate identifiers.\n"
                                          "    Identifier '{}' appeared more than once".format(a))
            name_set.add(a)
        for (n, _) in named_exprs.items():
            if n in name_set:
                raise ExpressionException("Cannot select and assign '{}' in the same 'select' call".format(n))

        selected_type = tstruct(**{f:self.dtype[f] for f in fields})
        selected_expr = construct_expr(SelectFields(self._ir, fields), selected_type, self._indices, self._aggregations)

        if len(named_exprs) == 0:
            return selected_expr
        else:
            return selected_expr.annotate(**named_exprs)

    @typecheck_method(fields=str)
    def drop(self, *fields):
        """Drop fields from the struct.

        Examples
        --------

        >>> hl.eval(struct.drop('b'))
        Struct(a=5)

        Parameters
        ----------
        fields: varargs of :obj:`str`
            Fields to drop.

        Returns
        -------
        :class:`.StructExpression`
            Struct without certain fields.
        """
        to_drop = set()
        for a in fields:
            if not a in self._fields:
                raise KeyError("Struct has no field '{}'\n"
                               "    Fields: [ {} ]".format(a, ', '.join("'{}'".format(x) for x in self._fields)))
            if a in to_drop:
                warn("Found duplicate field name in 'StructExpression.drop': '{}'".format(a))
            to_drop.add(a)

        to_keep = [f for f in self.dtype.keys() if f not in to_drop]
        return self.select(*to_keep)

    def flatten(self):
        def _flatten(prefix, s):
            if isinstance(s, StructExpression):
                return [(k, v) for (f, e) in s.items() for (k, v) in _flatten(prefix + '.' + f, e)]
            else:
                return [(prefix, s)]
        return self.select(**{k: v for (f, e) in self.items() for (k, v) in _flatten(f, e)})

    def _nested_summary(self, agg_result, top):
        sep = '' if top else '.'
        return {f'{sep}{k}': f._summarize(agg_result[k]) for k, f in self.items()}

    def _summary_aggs(self):
        return hl.struct(**{k: f._all_summary_aggs() for k, f in self.items()})


class TupleExpression(Expression, Sequence):
    """Expression of type :class:`.ttuple`.

    >>> tup = hl.literal(("a", 1, [1, 2, 3]))
    """

    @typecheck_method(item=oneof(int, slice))
    def __getitem__(self, item):
        """Index into the tuple.

        Examples
        --------

        >>> hl.eval(tup[1])
        1

        Parameters
        ----------
        item : :obj:`int`
            Element index.

        Returns
        -------
        :class:`.Expression`
        """
        if isinstance(item, slice):
            assert item.start is None or isinstance(item.start, int)
            assert item.stop is None or isinstance(item.stop, int)
            assert item.step is None or isinstance(item.step, int)
            return hl.or_missing(hl.is_defined(self),
                                 hl.tuple([
                                     self[i]
                                     for i in range(len(self))[item.start:item.stop:item.step]]))
        if not 0 <= item < len(self):
            raise IndexError("Out of bounds index. Tuple length is {}.".format(len(self)))
        return construct_expr(ir.GetTupleElement(self._ir, item), self.dtype.types[item], self._indices)

    def __len__(self):
        """Returns the length of the tuple.

        Examples
        --------

        >>> len(tup)
        3

        Returns
        -------
        :obj:`int`
        """
        return len(self.dtype.types)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def _nested_summary(self, agg_result, top):
        return {f'[{i}]': self[i]._summarize(agg_result[i]) for i in range(len(self))}

    def _summary_aggs(self):
        return hl.tuple([self[i]._all_summary_aggs() for i in range(len(self))])


class NumericExpression(Expression):
    """Expression of numeric type.

    >>> x = hl.literal(3)

    >>> y = hl.literal(4.5)
    """

    @typecheck_method(other=expr_numeric)
    def __lt__(self, other):
        """Less-than comparison.

        Examples
        --------

        >>> hl.eval(x < 5)
        True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is smaller than the right side.
        """
        return self._bin_op_numeric("<", other, lambda _: tbool)

    @typecheck_method(other=expr_numeric)
    def __le__(self, other):
        """Less-than-or-equals comparison.

        Examples
        --------

        >>> hl.eval(x <= 3)
        True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is smaller than or equal to the right side.
        """
        return self._bin_op_numeric("<=", other, lambda _: tbool)

    @typecheck_method(other=expr_numeric)
    def __gt__(self, other):
        """Greater-than comparison.

        Examples
        --------

        >>> hl.eval(y > 4)
        True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is greater than the right side.
        """
        return self._bin_op_numeric(">", other, lambda _: tbool)

    @typecheck_method(other=expr_numeric)
    def __ge__(self, other):
        """Greater-than-or-equals comparison.

        Examples
        --------

        >>> hl.eval(y >= 4)
        True

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Right side for comparison.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the left side is greater than or equal to the right side.
        """
        return self._bin_op_numeric(">=", other, lambda _: tbool)

    def __pos__(self):
        return self

    def __neg__(self):
        """Negate the number (multiply by -1).

        Examples
        --------

        >>> hl.eval(-x)
        -3

        Returns
        -------
        :class:`.NumericExpression`
            Negated number.
        """

        return expr_numeric.coerce(self)._unary_op("-")

    def __add__(self, other):
        """Add two numbers.

        Examples
        --------

        >>> hl.eval(x + 2)
        5

        >>> hl.eval(x + y)
        7.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to add.

        Returns
        -------
        :class:`.NumericExpression`
            Sum of the two numbers.
        """
        return self._bin_op_numeric("+", other)

    def __radd__(self, other):
        return self._bin_op_numeric_reverse("+", other)

    def __sub__(self, other):
        """Subtract the right number from the left.

        Examples
        --------

        >>> hl.eval(x - 2)
        1

        >>> hl.eval(x - y)
        -1.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to subtract.

        Returns
        -------
        :class:`.NumericExpression`
            Difference of the two numbers.
        """
        return self._bin_op_numeric("-", other)

    def __rsub__(self, other):
        return self._bin_op_numeric_reverse("-", other)

    def __mul__(self, other):
        """Multiply two numbers.

        Examples
        --------

        >>> hl.eval(x * 2)
        6

        >>> hl.eval(x * y)
        13.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Number to multiply.

        Returns
        -------
        :class:`.NumericExpression`
            Product of the two numbers.
        """
        return self._bin_op_numeric("*", other)

    def __rmul__(self, other):
        return self._bin_op_numeric_reverse("*", other)

    def __truediv__(self, other):
        """Divide two numbers.

        Examples
        --------

        >>> hl.eval(x / 2)
        1.5

        >>> hl.eval(y / 0.1)
        45.0

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            The left number divided by the left.
        """

        def ret_type_f(t):
            assert is_numeric(t)
            if t == tint32 or t == tint64:
                return tfloat32
            else:
                # Float64 or Float32
                return t

        return self._bin_op_numeric("/", other, ret_type_f)

    def __rtruediv__(self, other):
        def ret_type_f(t):
            assert is_numeric(t)
            if t == tint32 or t == tint64:
                return tfloat32
            else:
                # float64 or float32
                return t

        return self._bin_op_numeric_reverse("/", other, ret_type_f)

    def __floordiv__(self, other):
        """Divide two numbers with floor division.

        Examples
        --------

        >>> hl.eval(x // 2)
        1

        >>> hl.eval(y // 2)
        2.0

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            The floor of the left number divided by the right.
        """
        return self._bin_op_numeric('//', other)

    def __rfloordiv__(self, other):
        return self._bin_op_numeric_reverse('//', other)

    def __mod__(self, other):
        """Compute the left modulo the right number.

        Examples
        --------

        >>> hl.eval(32 % x)
        2

        >>> hl.eval(7 % y)
        2.5

        Parameters
        ----------
        other : :class:`.NumericExpression`
            Dividend.

        Returns
        -------
        :class:`.NumericExpression`
            Remainder after dividing the left by the right.
        """
        return self._bin_op_numeric('%', other)

    def __rmod__(self, other):
        return self._bin_op_numeric_reverse('%', other)

    def __pow__(self, power, modulo=None):
        """Raise the left to the right power.

        Examples
        --------

        >>> hl.eval(x ** 2)
        9.0

        >>> hl.eval(x ** -2)
        0.1111111111111111

        >>> hl.eval(y ** 1.5)
        9.545941546018392

        Parameters
        ----------
        power : :class:`.NumericExpression`
        modulo
            Unsupported argument.

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tfloat64`
            Result of raising left to the right power.
        """
        return self._bin_op_numeric('**', power, lambda _: tfloat64)

    def __rpow__(self, other):
        return self._bin_op_numeric_reverse('**', other, lambda _: tfloat64)


class BooleanExpression(NumericExpression):
    """Expression of type :py:data:`.tbool`.

    >>> t = hl.literal(True)
    >>> f = hl.literal(False)
    >>> na = hl.null(hl.tbool)

    >>> hl.eval(t)
    True

    >>> hl.eval(f)
    False

    >>> hl.eval(na)
    None

    """

    @typecheck_method(other=expr_bool)
    def __rand__(self, other):
        return self.__and__(other)

    @typecheck_method(other=expr_bool)
    def __ror__(self, other):
        return self.__or__(other)

    @typecheck_method(other=expr_bool)
    def __and__(self, other):
        """Return ``True`` if the left and right arguments are ``True``.

        Examples
        --------

        >>> hl.eval(t & f)
        False

        >>> hl.eval(t & na)
        None

        >>> hl.eval(f & na)
        False

        The ``&`` and ``|`` operators have higher priority than comparison
        operators like ``==``, ``<``, or ``>``. Parentheses are often
        necessary:

        >>> x = hl.literal(5)

        >>> hl.eval((x < 10) & (x > 2))
        True

        Parameters
        ----------
        other : :class:`.BooleanExpression`
            Right-side operand.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if both left and right are ``True``.
        """
        return self._method("&&", tbool, other)

    @typecheck_method(other=expr_bool)
    def __or__(self, other):
        """Return ``True`` if at least one of the left and right arguments is ``True``.

        Examples
        --------

        >>> hl.eval(t | f)
        True

        >>> hl.eval(t | na)
        True

        >>> hl.eval(f | na)
        None

        The ``&`` and ``|`` operators have higher priority than comparison
        operators like ``==``, ``<``, or ``>``. Parentheses are often
        necessary:

        >>> x = hl.literal(5)

        >>> hl.eval((x < 10) | (x > 20))
        True

        Parameters
        ----------
        other : :class:`.BooleanExpression`
            Right-side operand.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if either left or right is ``True``.
        """
        return self._method("||", tbool, other)

    def __invert__(self):
        """Return the boolean negation.

        Examples
        --------

        >>> hl.eval(~t)
        False

        >>> hl.eval(~f)
        True

        >>> hl.eval(~na)
        None

        Returns
        -------
        :class:`.BooleanExpression`
            Boolean negation.
        """
        return self._unary_op("!")

    def _extra_summary_fields(self, agg_result):
        return {'Counts': agg_result}

    def _summary_aggs(self):
        return hl.agg.filter(hl.is_defined(self), hl.agg.counter(self))


class Float64Expression(NumericExpression):
    """Expression of type :py:data:`.tfloat64`."""

    def _extra_summary_fields(self, agg_result):
        return {
            'Minimum': agg_result['min'],
            'Maximum': agg_result['max'],
            'Mean': agg_result['mean'],
            'Std Dev': agg_result['stdev']
        }

    def _summary_aggs(self):
        return hl.agg.stats(self)


class Float32Expression(NumericExpression):
    """Expression of type :py:data:`.tfloat32`."""

    def _extra_summary_fields(self, agg_result):
        return {
            'Minimum': agg_result['min'],
            'Maximum': agg_result['max'],
            'Mean': agg_result['mean'],
            'Std Dev': agg_result['stdev']
        }

    def _summary_aggs(self):
        return hl.agg.stats(self)


class Int32Expression(NumericExpression):
    """Expression of type :py:data:`.tint32`."""
    def _extra_summary_fields(self, agg_result):
        return {
            'Minimum': int(agg_result['min']),
            'Maximum': int(agg_result['max']),
            'Mean': agg_result['mean'],
            'Std Dev': agg_result['stdev']
        }

    def _summary_aggs(self):
        return hl.agg.stats(self)


class Int64Expression(NumericExpression):
    """Expression of type :py:data:`.tint64`."""
    def _extra_summary_fields(self, agg_result):
        return {
            'Minimum': int(agg_result['min']),
            'Maximum': int(agg_result['max']),
            'Mean': agg_result['mean'],
            'Std Dev': agg_result['stdev']
        }

    def _summary_aggs(self):
        return hl.agg.stats(self)


class StringExpression(Expression):
    """Expression of type :py:data:`.tstr`.

    >>> s = hl.literal('The quick brown fox')
    """

    def __getitem__(self, item):
        """Slice or index into the string.

        Examples
        --------

        >>> hl.eval(s[:15])
        'The quick brown'

        >>> hl.eval(s[0])
        'T'

        Parameters
        ----------
        item : slice or :class:`.Expression` of type :py:data:`.tint32`
            Slice or character index.

        Returns
        -------
        :class:`.StringExpression`
            Substring or character at index `item`.
        """
        if isinstance(item, slice):
            return self._slice(tstr, item.start, item.stop, item.step)
        else:
            item = to_expr(item)
            if not item.dtype == tint32:
                raise TypeError("String expects index to be type 'slice' or expression of type 'int32', "
                                "found expression of type '{}'".format(item.dtype))
            return self._index(tstr, item)

    def __add__(self, other):
        """Concatenate strings.

        Examples
        --------

        >>> hl.eval(s + ' jumped over the lazy dog')
        'The quick brown fox jumped over the lazy dog'

        Parameters
        ----------
        other : :class:`.StringExpression`
            String to concatenate.

        Returns
        -------
        :class:`.StringExpression`
            Concatenated string.
        """
        other = to_expr(other)
        if not other.dtype == tstr:
            raise NotImplementedError("'{}' + '{}'".format(self.dtype, other.dtype))
        return self._bin_op("+", other, self.dtype)

    def __radd__(self, other):
        other = to_expr(other)
        if not other.dtype == tstr:
            raise NotImplementedError("'{}' + '{}'".format(other.dtype, self.dtype))
        return self._bin_op_reverse("+", other, self.dtype)

    def length(self):
        """Returns the length of the string.

        Examples
        --------

        >>> hl.eval(s.length())
        19

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            Length of the string.
        """
        return apply_expr(lambda x: Apply("length", tint32, x), tint32, self)

    @typecheck_method(pattern1=expr_str, pattern2=expr_str)
    def replace(self, pattern1, pattern2):
        """Replace substrings matching `pattern1` with `pattern2` using regex.

        Examples
        --------

        Replace spaces with underscores in a Hail string:

        >>> hl.eval(hl.str("The quick  brown fox").replace(' ', '_'))
        'The_quick__brown_fox'

        Remove the leading zero in contigs in variant strings in a table:

        >>> t = hl.import_table('data/leading-zero-variants.txt')
        >>> t.show()
        +----------------+
        | variant        |
        +----------------+
        | str            |
        +----------------+
        | "01:1000:A:T"  |
        | "01:10001:T:G" |
        | "02:99:A:C"    |
        | "02:893:G:C"   |
        | "22:100:A:T"   |
        | "X:10:C:A"     |
        +----------------+
        <BLANKLINE>
        >>> t = t.annotate(variant = t.variant.replace("^0([0-9])", "$1"))
        >>> t.show()
        +---------------+
        | variant       |
        +---------------+
        | str           |
        +---------------+
        | "1:1000:A:T"  |
        | "1:10001:T:G" |
        | "2:99:A:C"    |
        | "2:893:G:C"   |
        | "22:100:A:T"  |
        | "X:10:C:A"    |
        +---------------+
        <BLANKLINE>

        Notes
        -----

        The regex expressions used should follow `Java regex syntax
        <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`_. In
        the Java regular expression syntax, a dollar sign, ``$1``, refers to the
        first group, not the canonical ``\\1``.

        Parameters
        ----------
        pattern1 : str or :class:`.StringExpression`
        pattern2 : str or :class:`.StringExpression`

        Returns
        -------
        """
        return self._method("replace", tstr, pattern1, pattern2)

    @typecheck_method(delim=expr_str, n=nullable(expr_int32))
    def split(self, delim, n=None):
        """Returns an array of strings generated by splitting the string at `delim`.

        Examples
        --------

        >>> hl.eval(s.split('\\s+'))
        ['The', 'quick', 'brown', 'fox']

        >>> hl.eval(s.split('\\s+', 2))
        ['The', 'quick brown fox']

        Notes
        -----
        The delimiter is a regex using the
        `Java regex syntax <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`_
        delimiter. To split on special characters, escape them with double
        backslash (``\\\\``).

        Parameters
        ----------
        delim : str or :class:`.StringExpression`
            Delimiter regex.
        n : :class:`.Expression` of type :py:data:`.tint32`, optional
            Maximum number of splits.

        Returns
        -------
        :class:`.ArrayExpression`
            Array of split strings.
        """
        if n is None:
            return self._method("split", tarray(tstr), delim)
        else:
            return self._method("split", tarray(tstr), delim, n)

    def lower(self):
        """Returns a copy of the string, but with upper case letters converted
        to lower case.

        Examples
        --------

        >>> hl.eval(s.lower())
        'the quick brown fox'

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method("lower", tstr)

    def upper(self):
        """Returns a copy of the string, but with lower case letters converted
        to upper case.

        Examples
        --------

        >>> hl.eval(s.upper())
        'THE QUICK BROWN FOX'

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method("upper", tstr)

    def strip(self):
        r"""Returns a copy of the string with whitespace removed from the start
        and end.

        Examples
        --------

        >>> s2 = hl.str('  once upon a time\n')
        >>> hl.eval(s2.strip())
        'once upon a time'

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method("strip", tstr)

    @typecheck_method(substr=expr_str)
    def contains(self, substr):
        """Returns whether `substr` is contained in the string.

        Examples
        --------

        >>> hl.eval(s.contains('fox'))
        True

        >>> hl.eval(s.contains('dog'))
        False

        Note
        ----
        This method is case-sensitive.

        Parameters
        ----------
        substr : :class:`.StringExpression`

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("contains", tbool, substr)

    @typecheck_method(substr=expr_str)
    def startswith(self, substr):
        """Returns whether `substr` is a prefix of the string.

        Examples
        --------

        >>> hl.eval(s.startswith('The'))
        True

        >>> hl.eval(s.startswith('the'))
        False

        Note
        ----
        This method is case-sensitive.

        Parameters
        ----------
        substr : :class:`.StringExpression`

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method('startswith', tbool, substr)


    @typecheck_method(substr=expr_str)
    def endswith(self, substr):
        """Returns whether `substr` is a suffix of the string.

        Examples
        --------

        >>> hl.eval(s.endswith('fox'))
        True

        Note
        ----
        This method is case-sensitive.

        Parameters
        ----------
        substr : :class:`.StringExpression`

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method('endswith', tbool, substr)

    @typecheck_method(regex=str)
    def first_match_in(self, regex):
        """Returns an array containing the capture groups of the first match of
        `regex` in the given character sequence.

        Examples
        --------

        >>> hl.eval(s.first_match_in("The quick (\\w+) fox"))
        ['brown']

        >>> hl.eval(s.first_match_in("The (\\w+) (\\w+) (\\w+)"))
        ['quick', 'brown', 'fox']

        >>> hl.eval(s.first_match_in("(\\w+) (\\w+)"))
        ['The', 'quick']

        Parameters
        ----------
        regex : :class:`.StringExpression`

        Returns
        -------
        :class:`.ArrayExpression` with element type :py:data:`.tstr`
        """
        return self._method('firstMatchIn', tarray(tstr), regex)

    @typecheck_method(mapping=expr_dict(expr_str, expr_str))
    def translate(self, mapping):
        """Translates characters of the string using `mapping`.

        Examples
        --------
        >>> string = hl.literal('ATTTGCA')
        >>> hl.eval(string.translate({'T': 'U'}))
        'AUUUGCA'

        Parameters
        ----------
        mapping : :class:`.DictExpression`
            Dictionary of character-character translations.

        Returns
        -------
        :class:`.StringExpression`

        See Also
        --------
        :meth:`.replace`
        """
        return self._method('translate', tstr, mapping)

    @typecheck_method(regex=str)
    def matches(self, regex):
        """Returns ``True`` if the string contains any match for the given regex.

        Examples
        --------

        >>> string = hl.literal('NA12878')

        The `regex` parameter does not need to match the entire string:

        >>> hl.eval(string.matches('12'))
        True

        Regex motifs can be used to match sequences of characters:

        >>> hl.eval(string.matches(r'NA\\d+'))
        True

        Notes
        -----
        The `regex` argument is a
        `regular expression <https://en.wikipedia.org/wiki/Regular_expression>`__,
        and uses
        `Java regex syntax <https://docs.oracle.com/javase/8/docs/api/java/util/regex/Pattern.html>`__.

        Parameters
        ----------
        regex: :obj:`str`
            Pattern to match.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the string contains any match for the regex, otherwise ``False``.
        """
        return to_expr(regex, tstr)._method("~", tbool, self)

    def reverse(self):
        """Returns the reversed value.
        Examples
        --------

        >>> string = hl.literal('ATGCC')
        >>> hl.eval(string.reverse())
        'CCGTA'

        Returns
        -------
        :class:`.StringExpression`
        """
        return self._method('reverse', tstr)

    def _extra_summary_fields(self, agg_result):
        return {
            'Min Size': agg_result[0],
            'Max Size': agg_result[1],
            'Mean Size': agg_result[2],
            'Sample Values': agg_result[3],
        }

    def _summary_aggs(self):
        length = hl.len(self)
        return hl.tuple((
            hl.agg.min(length),
            hl.agg.max(length),
            hl.agg.mean(length),
            hl.agg.filter(hl.is_defined(self), hl.agg.take(self, 5))))


class CallExpression(Expression):
    """Expression of type :py:data:`.tcall`.

    >>> call = hl.call(0, 1, phased=False)
    """

    def __getitem__(self, item):
        """Get the i*th* allele.

        Examples
        --------

        Index with a single integer:

        >>> hl.eval(call[0])
        0

        >>> hl.eval(call[1])
        1

        Parameters
        ----------
        item : int or :class:`.Expression` of type :py:data:`.tint32`
            Allele index.

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
        """
        if isinstance(item, slice):
            raise NotImplementedError("CallExpression does not support indexing with a slice.")
        else:
            item = to_expr(item)
            if not item.dtype == tint32:
                raise TypeError("Call expects allele index to be an expression of type 'int32', "
                                "found expression of type '{}'".format(item.dtype))
            return self._index(tint32, item)

    @property
    def ploidy(self):
        """Return the number of alleles of this call.

        Examples
        --------

        >>> hl.eval(call.ploidy)
        2

        Notes
        -----
        Currently only ploidy 1 and 2 are supported.

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
        """
        return self._method("ploidy", tint32)

    @property
    def phased(self):
        """True if the call is phased.

        Examples
        --------

        >>> hl.eval(call.phased)
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isPhased", tbool)

    def is_haploid(self):
        """True if the call has ploidy equal to 1.

        Examples
        --------

        >>> hl.eval(call.is_haploid())
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self.ploidy == 1

    def is_diploid(self):
        """True if the call has ploidy equal to 2.

        Examples
        --------

        >>> hl.eval(call.is_diploid())
        True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self.ploidy == 2

    def is_non_ref(self):
        """Evaluate whether the call includes one or more non-reference alleles.

        Examples
        --------

        >>> hl.eval(call.is_non_ref())
        True

        Notes
        -----
        In the diploid biallelic case, a ``0/0`` call will return ``False``,
        and ``0/1`` and ``1/1`` will return ``True``.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if at least one allele is non-reference, ``False`` otherwise.
        """
        return self._method("isNonRef", tbool)

    def is_het(self):
        """Evaluate whether the call includes two different alleles.

        Examples
        --------

        >>> hl.eval(call.is_het())
        True

        Notes
        -----
        In the diploid biallelic case, a ``0/1`` call will return ``True``,
        and ``0/0`` and ``1/1`` will return ``False``.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the two alleles are different, ``False`` if they are the same.
        """
        return self._method("isHet", tbool)

    def is_het_non_ref(self):
        """Evaluate whether the call includes two different alleles, neither of which is reference.

        Examples
        --------

        >>> hl.eval(call.is_het_non_ref())
        False

        Notes
        -----
        A biallelic variant may never have a het-non-ref call. Examples of
        these calls are ``1/2`` and ``2/4``.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two different alternate alleles, ``False`` otherwise.
        """
        return self._method("isHetNonRef", tbool)

    def is_het_ref(self):
        """Evaluate whether the call includes two different alleles, one of which is reference.

        Examples
        --------

        >>> hl.eval(call.is_het_ref())
        True

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes one reference and one alternate allele, ``False`` otherwise.
        """
        return self._method("isHetRef", tbool)

    def is_hom_ref(self):
        """Evaluate whether the call includes two reference alleles.

        Examples
        --------

        >>> hl.eval(call.is_hom_ref())
        False

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two reference alleles, ``False`` otherwise.
        """
        return self._method("isHomRef", tbool)

    def is_hom_var(self):
        """Evaluate whether the call includes two identical alternate alleles.

        Examples
        --------

        >>> hl.eval(call.is_hom_var())
        False

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if the call includes two identical alternate alleles, ``False`` otherwise.
        """
        return self._method("isHomVar", tbool)

    def n_alt_alleles(self):
        """Returns the number of non-reference alleles.

        Examples
        --------

        >>> hl.eval(call.n_alt_alleles())
        1

        Notes
        -----
        For diploid biallelic calls, this method is equivalent to the alternate
        allele dosage. For instance, ``0/0`` will return ``0``, ``0/1`` will
        return ``1``, and ``1/1`` will return ``2``.

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            The number of non-reference alleles.
        """
        return self._method("nNonRefAlleles", tint32)

    @typecheck_method(alleles=expr_array(expr_str))
    def one_hot_alleles(self, alleles):
        """Returns an array containing the summed one-hot encoding of the
        alleles.

        Examples
        --------

        >>> hl.eval(call.one_hot_alleles(['A', 'T']))
        [1, 1]

        This one-hot representation is the positional sum of the one-hot
        encoding for each called allele. For a biallelic variant, the one-hot
        encoding for a reference allele is ``[1, 0]`` and the one-hot encoding
        for an alternate allele is ``[0, 1]``. Diploid calls would produce the
        following arrays: ``[2, 0]`` for homozygous reference, ``[1, 1]`` for
        heterozygous, and ``[0, 2]`` for homozygous alternate.

        Parameters
        ----------
        alleles: :class:`.ArrayStringExpression`
            Variant alleles.

        Returns
        -------
        :class:`.ArrayInt32Expression`
            An array of summed one-hot encodings of allele indices.
        """
        return self._method("oneHotAlleles", tarray(tint32), hl.len(alleles))

    def unphased_diploid_gt_index(self):
        """Return the genotype index for unphased, diploid calls.

        Examples
        --------

        >>> hl.eval(call.unphased_diploid_gt_index())
        1

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
        """
        return self._method("unphasedDiploidGtIndex", tint32)

    def _extra_summary_fields(self, agg_result):
        return {
            'Homozygous Reference': agg_result[0],
            'Heterozygous': agg_result[1],
            'Homozygous Variant': agg_result[2],
            'Ploidy': agg_result[3],
            'Phased': agg_result[4]
        }

    def _summary_aggs(self):
        return hl.tuple((
            hl.agg.count_where(self.is_hom_ref()),
            hl.agg.count_where(self.is_het()),
            hl.agg.count_where(self.is_hom_var()),
            hl.agg.filter(hl.is_defined(self), hl.agg.counter(self.ploidy)),
            hl.agg.filter(hl.is_defined(self), hl.agg.counter(self.phased))))


class LocusExpression(Expression):
    """Expression of type :class:`.tlocus`.

    >>> locus = hl.locus('1', 1034245)
    """

    @property
    def contig(self):
        """Returns the chromosome.

        Examples
        --------

        >>> hl.eval(locus.contig)
        '1'

        Returns
        -------
        :class:`.StringExpression`
            The chromosome for this locus.
        """
        return self._method("contig", tstr)

    @property
    def position(self):
        """Returns the position along the chromosome.

        Examples
        --------

        >>> hl.eval(locus.position)
        1034245

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint32`
            This locus's position along its chromosome.
        """
        return self._method("position", tint32)

    def global_position(self):
        """Returns a zero-indexed absolute position along the reference genome.

        The global position is computed as :py:attr:`~position` - 1 plus the sum
        of the lengths of all the contigs that precede this locus's :py:attr:`~contig`
        in the reference genome's ordering of contigs.

        See also :func:`.locus_from_global_position`.

        Examples
        --------
        A locus with position 1 along chromosome 1 will have a global position of 0 along
        the reference genome GRCh37.

        >>> hl.eval(hl.locus('1', 1).global_position())
        0

        A locus with position 1 along chromosome 2 will have a global position of (1-1) + 249250621,
        where 249250621 is the length of chromosome 1 on GRCh37.

        >>> hl.eval(hl.locus('2', 1).global_position())
        249250621

        A different reference genome than the default results in a different global position.

        >>> hl.eval(hl.locus('chr2', 1, 'GRCh38').global_position())
        248956422

        Returns
        -------
        :class:`.Expression` of type :py:data:`.tint64`
            Global base position of locus along the reference genome.
        """
        return self._method('locusToGlobalPos', tint64)

    def in_x_nonpar(self):
        """Returns ``True`` if the locus is in a non-pseudoautosomal
        region of chromosome X.

        Examples
        --------

        >>> hl.eval(locus.in_x_nonpar())
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inXNonPar", tbool)

    def in_x_par(self):
        """Returns ``True`` if the locus is in a pseudoautosomal region
        of chromosome X.

        Examples
        --------

        >>> hl.eval(locus.in_x_par())
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inXPar", tbool)

    def in_y_nonpar(self):
        """Returns ``True`` if the locus is in a non-pseudoautosomal
        region of chromosome Y.

        Examples
        --------

        >>> hl.eval(locus.in_y_nonpar())
        False

        Note
        ----
        Many variant callers only generate variants on chromosome X for the
        pseudoautosomal region. In this case, all loci mapped to chromosome
        Y are non-pseudoautosomal.

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inYNonPar", tbool)

    def in_y_par(self):
        """Returns ``True`` if the locus is in a pseudoautosomal region
        of chromosome Y.

        Examples
        --------

        >>> hl.eval(locus.in_y_par())
        False

        Note
        ----
        Many variant callers only generate variants on chromosome X for the
        pseudoautosomal region. In this case, all loci mapped to chromosome
        Y are non-pseudoautosomal.

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("inYPar", tbool)

    def in_autosome(self):
        """Returns ``True`` if the locus is on an autosome.

        Notes
        -----
        All contigs are considered autosomal except those
        designated as X, Y, or MT by :class:`.ReferenceGenome`.

        Examples
        --------

        >>> hl.eval(locus.in_autosome())
        True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isAutosomal", tbool)

    def in_autosome_or_par(self):
        """Returns ``True`` if the locus is on an autosome or
        a pseudoautosomal region of chromosome X or Y.

        Examples
        --------

        >>> hl.eval(locus.in_autosome_or_par())
        True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isAutosomalOrPseudoAutosomal", tbool)

    def in_mito(self):
        """Returns ``True`` if the locus is on mitochondrial DNA.

        Examples
        --------

        >>> hl.eval(locus.in_mito())
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("isMitochondrial", tbool)

    @typecheck_method(before=expr_int32, after=expr_int32)
    def sequence_context(self, before=0, after=0):
        """Return the reference genome sequence at the locus.

        Examples
        --------

        Get the reference allele at a locus:

        >>> hl.eval(locus.sequence_context()) # doctest: +SKIP
        "G"

        Get the reference sequence at a locus including the previous 5 bases:

        >>> hl.eval(locus.sequence_context(before=5)) # doctest: +SKIP
        "ACTCGG"

        Notes
        -----
        This function requires that this locus' reference genome has an attached
        reference sequence. Use :meth:`.ReferenceGenome.add_sequence` to
        load and attach a reference sequence to a reference genome.

        Parameters
        ----------
        before : :class:`.Expression` of type :py:data:`.tint32`, optional
            Number of bases to include before the locus. Truncates at
            contig boundary.
        after : :class:`.Expression` of type :py:data:`.tint32`, optional
            Number of bases to include after the locus. Truncates at
            contig boundary.

        Returns
        -------
        :class:`.StringExpression`
        """

        rg = self.dtype.reference_genome
        if not rg.has_sequence():
            raise TypeError("Reference genome '{}' does not have a sequence loaded. Use 'add_sequence' to load the sequence from a FASTA file.".format(rg.name))
        return hl.get_sequence(self.contig, self.position, before, after, rg)

    @typecheck_method(before=expr_int32, after=expr_int32)
    def window(self, before, after):
        """Returns an interval of a specified number of bases around the locus.

        Examples
        --------
        Create a window of two megabases centered at a locus:

        >>> locus = hl.locus('16', 29_500_000)
        >>> window = locus.window(1_000_000, 1_000_000)
        >>> hl.eval(window)
        Interval(start=Locus(contig=16, position=28500000, reference_genome=GRCh37), end=Locus(contig=16, position=30500000, reference_genome=GRCh37), includes_start=True, includes_end=True)

        Notes
        -----
        The returned interval is inclusive of both the `start` and `end`
        endpoints.

        Parameters
        ----------
        before : :class:`.Expression` of type :py:data:`.tint32`
            Number of bases to include before the locus. Truncates at 1.
        after : :class:`.Expression` of type :py:data:`.tint32`
            Number of bases to include after the locus. Truncates at
            contig length.

        Returns
        -------
        :class:`.IntervalExpression`
        """
        start_pos = hl.max(1, self.position - before)
        end_pos = hl.min(hl.contig_length(self.contig, self.dtype.reference_genome), self.position + after)
        return hl.interval(start=hl.locus(self.contig, start_pos),
                           end=hl.locus(self.contig, end_pos),
                           includes_start=True,
                           includes_end=True)

    def _extra_summary_fields(self, agg_result):
        return {'Contig Counts': agg_result}

    def _summary_aggs(self):
        return hl.agg.filter(hl.is_defined(self), hl.agg.counter(self.contig))


class IntervalExpression(Expression):
    """Expression of type :class:`.tinterval`.

    >>> interval = hl.interval(3, 11)
    >>> locus_interval = hl.parse_locus_interval("1:53242-90543")
    """

    @typecheck_method(value=expr_any)
    def contains(self, value):
        """Tests whether a value is contained in the interval.

        Examples
        --------

        >>> hl.eval(interval.contains(3))
        True

        >>> hl.eval(interval.contains(11))
        False

        Parameters
        ----------
        value :
            Object with type matching the interval point type.

        Returns
        -------
        :class:`.BooleanExpression`
            ``True`` if `value` is contained in the interval, ``False`` otherwise.
        """
        if self.dtype.point_type != value.dtype:
            raise TypeError("expected '{}', found: '{}'".format(self.dtype.point_type, value.dtype))
        return self._method("contains", tbool, value)

    @typecheck_method(interval=expr_interval(expr_any))
    def overlaps(self, interval):
        """True if the the supplied interval contains any value in common with this one.

        Examples
        --------

        >>> hl.eval(interval.overlaps(hl.interval(5, 9)))
        True

        >>> hl.eval(interval.overlaps(hl.interval(11, 20)))
        False

        Parameters
        ----------
        interval : :class:`.Expression` with type :py:data:`.tinterval`
            Interval object with the same point type.

        Returns
        -------
        :class:`.BooleanExpression`
        """
        if self.dtype.point_type != interval.dtype.point_type:
            raise TypeError("expected '{}', found: '{}'".format(self.dtype.point_type, interval.dtype.point_type))
        return self._method("overlaps", tbool, interval)

    @property
    def end(self):
        """Returns the end point.

        Examples
        --------

        >>> hl.eval(interval.end)
        11

        Returns
        -------
        :class:`.Expression`
        """
        return self._method("end", self.dtype.point_type)

    @property
    def start(self):
        """Returns the start point.

        Examples
        --------

        >>> hl.eval(interval.start)
        3

        Returns
        -------
        :class:`.Expression`
        """
        return self._method("start", self.dtype.point_type)

    @property
    def includes_start(self):
        """True if the interval includes the start point.

        Examples
        --------

        >>> hl.eval(interval.includes_start)
        True

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("includesStart", tbool)

    @property
    def includes_end(self):
        """True if the interval includes the end point.

        Examples
        --------

        >>> hl.eval(interval.includes_end)
        False

        Returns
        -------
        :class:`.BooleanExpression`
        """
        return self._method("includesEnd", tbool)


class NDArrayExpression(Expression):
    """Expression of type :class:`.tndarray`.

    >>> nd = hl._nd.array([[1, 2], [3, 4]])
    """

    @property
    def ndim(self):
        """The number of dimensions of this ndarray.

        Examples
        --------

        >>> nd.ndim
        2

        Returns
        -------
        :obj:`int`
        """
        return self._type.ndim

    @property
    def T(self):
        """Reverse the dimensions of this ndarray. For an n-dimensional array `a`,
        a[i_0, ..., i_n-1, i_n] = a.T[i_n, i_n-1, ..., i_0].
        Same as `self.transpose()`.

        See also :func:`.transpose`.

        Returns
        -------
        :class:`.NDArrayExpression`.
        """
        return self.transpose()

    @typecheck_method(axes=nullable(tupleof(int)))
    def transpose(self, axes=None):
        """Permute the dimensions of this ndarray according to the ordering of `axes`. Axis `j` in the `i`th index of
        `axes` maps the `j`th dimension of the ndarray to the `i`th dimension of the output ndarray.

        Parameters
        ----------
        axes : :obj:`tuple` of :obj:`int`, optional
            The new ordering of the ndarray's dimensions.

        Notes
        -----
        Does nothing on ndarrays of dimensionality 0 or 1.

        Returns
        -------
        :class:`.NDArrayExpression`.
        """
        if axes is None:
            axes = list(reversed(range(self.ndim)))
        else:
            if len(axes) != self.ndim:
                raise ValueError(f'Must specify a complete permutation of the dimensions. '
                                 f'Expected {self.ndim} axes, got {len(axes)}')

            if len(set(axes)) != len(axes):
                raise ValueError(f'Axes cannot contain duplicates: {axes}')

            for axis in axes:
                if not 0 <= axis < self.ndim:
                    raise ValueError(f'Invalid axis: {axis}')

        if self.ndim < 2:
            return self

        return construct_expr(ir.NDArrayReindex(self._ir, axes), self._type, self._indices, self._aggregations)

    @property
    def shape(self):
        """The shape of this ndarray.

        Examples
        --------
        >>> hl.eval(nd.shape)
        (2, 2)

        Returns
        -------
        :class:`.TupleExpression`
        """
        shape_type = ttuple(*[tint64 for _ in range(self.ndim)])
        return construct_expr(NDArrayShape(self._ir), shape_type, self._indices, self._aggregations)

    opt_long_slice_ = sliceof(nullable(expr_int64), nullable(expr_int64), nullable(expr_int64))

    @typecheck_method(item=oneof(expr_int64, opt_long_slice_, tupleof(oneof(expr_int64, opt_long_slice_))))
    def __getitem__(self, item):
        if not isinstance(item, tuple):
            item = (item,)

        if len(item) != self.ndim:
            raise ValueError(f'Must specify one index per dimension. '
                             f'Expected {self.ndim} dimensions but got {len(item)}')

        n_sliced_dims = len([s for s in item if isinstance(s, slice)])
        if n_sliced_dims > 0:
            slices = []
            for i, s in enumerate(item):
                if isinstance(s, slice):
                    step = hl.case().when(s.step != 0, s.step).or_error("Slice step cannot be zero") if s.step is not None else to_expr(1, tint64)
                    start = hl.cond(s.start >= 0, s.start, self.shape[i] + s.start) if s.start is not None else hl.cond(step >= 0, to_expr(0, tint64), self.shape[i] - 1)
                    stop = hl.cond(s.stop >= 0, s.stop, self.shape[i] + s.stop) if s.stop is not None else hl.cond(step >= 0, self.shape[i], to_expr(-1, tint64))
                    slices.append(hl.tuple((start, stop, step)))
                else:
                    slices.append(s)
            return construct_expr(ir.NDArraySlice(self._ir, hl.tuple(slices)._ir),
                                  tndarray(self._type.element_type, n_sliced_dims),
                                  self._indices,
                                  self._aggregations)

        return construct_expr(ir.NDArrayRef(self._ir, [idx._ir for idx in item]), self._type.element_type)

    @typecheck_method(shape=oneof(expr_int64, tupleof(expr_int64), expr_tuple()))
    def reshape(self, shape):
        """Reshape this ndarray to a new shape.

        Parameters
        ----------
        shape : :class:`.Expression` of type :py:data:`.tint64` or
                :obj: `tuple` of :class:`.Expression` of type :py:data:`.tint64`

        Examples
        --------

        >>> v = hl._nd.array([1, 2, 3, 4]) # doctest: +SKIP
        >>> m = v.reshape((2, 2)) # doctest: +SKIP

        Returns
        -------
        :class:`.NDArrayExpression`.
        """
        if isinstance(shape, TupleExpression):
            shape_ir = hl.or_missing(hl.is_defined(shape), hl.tuple([hl.int64(i) for i in shape]))._ir
            ndim = len(shape)
        else:
            wrapped_shape = wrap_to_list(shape)
            ndim = len(wrapped_shape)
            shape_ir = hl.tuple(wrapped_shape)._ir

        return construct_expr(NDArrayReshape(self._ir, shape_ir),
                              tndarray(self._type.element_type, ndim),
                              self._indices,
                              self._aggregations)

    @typecheck_method(f=func_spec(1, expr_any))
    def map(self, f):
        """Transform each element of an NDArray.

        Parameters
        ----------
        f : function ( (arg) -> :class:`.Expression`)
            Function to transform each element of the NDArray.

        Returns
        -------
        :class:`.NDArrayExpression`.
            NDArray where each element has been transformed according to `f`.
        """

        element_type = self._type.element_type
        ndarray_map = self._ir_lambda_method(NDArrayMap, f, element_type, lambda t: tndarray(t, self.ndim))

        assert isinstance(self._type, tndarray)
        return ndarray_map

    def _broadcast_to_same_ndim(self, other):
        if isinstance(other, NDArrayExpression):
            if self.ndim < other.ndim:
                return self._broadcast(other.ndim), other
            elif self.ndim > other.ndim:
                return self, other._broadcast(self.ndim)

        return self, other

    def _broadcast(self, n_output_dims):
        assert self.ndim < n_output_dims

        # Right-align existing dimensions and start prepending new ones
        # to the left: e.g. [0, 1] -> [3, 2, 0, 1]
        # Based off numpy broadcasting with the assumption that everything
        # can be thought to have an infinite number of 1-length dimensions
        # prepended
        old_dims = range(self.ndim)
        new_dims = range(self.ndim, n_output_dims)
        idx_mapping = list(reversed(new_dims)) + list(old_dims)

        return construct_expr(NDArrayReindex(self._ir, idx_mapping),
                              tndarray(self._type.element_type, n_output_dims),
                              self._indices, self._aggregations)


class NDArrayNumericExpression(NDArrayExpression):
    """Expression of type :class:`.tndarray` with a numeric element type.

    Numeric ndarrays support arithmetic both with scalar values and other
    arrays. Arithmetic between two numeric ndarrays requires that the shapes of
    each ndarray be either identical or compatible for broadcasting. Operations
    are applied positionally (``nd1 * nd2`` will multiply the first element of
    ``nd1`` by the first element of ``nd2``, the second element of ``nd1`` by
    the second element of ``nd2``, and so on). Arithmetic with a scalar will
    apply the operation to each element of the ndarray.
    """

    def _bin_op_numeric(self, name, other, ret_type_f=None):
        if isinstance(other, list) or isinstance(other, np.ndarray):
            other = hl._nd.array(other)

        self_broadcast, other_broadcast = self._broadcast_to_same_ndim(other)
        return super(NDArrayNumericExpression, self_broadcast)._bin_op_numeric(name, other_broadcast, ret_type_f)

    def _bin_op_numeric_reverse(self, name, other, ret_type_f=None):
        if isinstance(other, list) or isinstance(other, np.ndarray):
            other = hl._nd.array(other)

        self_broadcast, other_broadcast = self._broadcast_to_same_ndim(other)
        return super(NDArrayNumericExpression, self_broadcast)._bin_op_numeric_reverse(name, other_broadcast, ret_type_f)

    def __neg__(self):
        """Negate elements of the ndarray.

        Returns
        -------
        :class:`.NDArrayNumericExpression`
            Array expression of the same type.
        """
        return self * -1

    def __add__(self, other):
        """Positionally add an array or a scalar.

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.NDArrayNumericExpression`
            Value or ndarray to add.

        Returns
        -------
        :class:`.NDArrayNumericExpression`
            NDArray of positional sums.
        """
        return self._bin_op_numeric("+", other)

    def __radd__(self, other):
        return self._bin_op_numeric_reverse("+", other)

    def __sub__(self, other):
        """Positionally subtract a ndarray or a scalar.

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.NDArrayNumericExpression`
            Value or ndarray to subtract.

        Returns
        -------
        :class:`.NDArrayNumericExpression`
            NDArray of positional differences.
        """
        return self._bin_op_numeric("-", other)

    def __rsub__(self, other):
        return self._bin_op_numeric_reverse("-", other)

    def __mul__(self, other):
        """Positionally multiply by a ndarray or a scalar.

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.NDArrayNumericExpression`
            Value or ndarray to multiply by.

        Returns
        -------
        :class:`.NDArrayNumericExpression`
            NDArray of positional products.
        """
        return self._bin_op_numeric("*", other)

    def __rmul__(self, other):
        return self._bin_op_numeric_reverse("*", other)

    def __truediv__(self, other):
        """Positionally divide by a ndarray or a scalar.

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.NDArrayNumericExpression`
            Value or ndarray to divide by.

        Returns
        -------
        :class:`.NDArrayNumericExpression`
            NDArray of positional quotients.
        """
        return self._bin_op_numeric("/", other, self._div_ret_type_f)

    def __rtruediv__(self, other):
        return self._bin_op_numeric_reverse("/", other, self._div_ret_type_f)

    def __floordiv__(self, other):
        """Positionally divide by a ndarray or a scalar using floor division.

        Parameters
        ----------
        other : :class:`.NumericExpression` or :class:`.NDArrayNumericExpression`

        Returns
        -------
        :class:`.NDArrayNumericExpression`
        """
        return self._bin_op_numeric('//', other)

    def __rfloordiv__(self, other):
        return self._bin_op_numeric_reverse('//', other)

    def __matmul__(self, other):
        """Matrix multiplication: `a @ b`, semantically equivalent to `NumPy` matmul. If `a` and `b` are vectors,
        the vector dot product is performed, returning a `NumericExpression`. If `a` and `b` are both 2-dimensional
        matrices, this performs normal matrix multiplication. If `a` and `b` have more than 2 dimensions, they are
        treated as multi-dimensional stacks of 2-dimensional matrices. Matrix multiplication is applied element-wise
        across the higher dimensions. E.g. if `a` has shape `(3, 4, 5)` and `b` has shape `(3, 5, 6)`, `a` is treated
        as a stack of three matrices of shape `(4, 5)` and `b` as a stack of three matrices of shape `(5, 6)`. `a @ b`
        would then have shape `(3, 4, 6)`.

        Notes
        -----
        The last dimension of `a` and the second to last dimension of `b` (or only dimension if `b` is a vector)
        must have the same length. The dimensions to the left of the last two dimensions of `a` and `b` (for NDArrays
        of dimensionality > 2) must be equal or be compatible for broadcasting.
        Number of dimensions of both NDArrays must be at least 1.

        Parameters
        ----------
        other : :class:`numpy.ndarray` :class:`.NDArrayNumericExpression`

        Returns
        -------
        :class:`.NDArrayNumericExpression` or :class:`.NumericExpression`
        """
        if not isinstance(other, NDArrayNumericExpression):
            other = hl._nd.array(other)

        if self.ndim == 0 or other.ndim == 0:
            raise ValueError('MatMul must be between objects of 1 dimension or more. Try * instead')

        if self.ndim > 1 and other.ndim > 1:
            left, right = self._broadcast_to_same_ndim(other)
        else:
            left, right = self, other

        from hail.linalg.utils.misc import _ndarray_matmul_ndim
        result_ndim = _ndarray_matmul_ndim(left.ndim, right.ndim)
        elem_type = unify_types(self._type.element_type, other._type.element_type)
        ret_type = tndarray(elem_type, result_ndim)
        left = left._promote_numeric(ret_type)
        right = right._promote_numeric(ret_type)

        res = construct_expr(NDArrayMatMul(left._ir, right._ir), ret_type, self._indices, self._aggregations)

        return res if result_ndim > 0 else res[()]

    @typecheck_method(axis=nullable(oneof(int, sequenceof(int))))
    def sum(self, axis=None):
        """Sum along one or more dimensions of the ndarray. If no axes are given, the entire NDArray will
        be summed to a single `NumericExpression`.

        Parameters
        ----------
        axis : :obj: `int` or :obj: `list` of :obj: `int:, optional

        Returns
        -------
        :class:`.NDArrayNumericExpression`
        """
        if axis is None:
            axes = list(range(self.ndim))
        else:
            axes = wrap_to_list(axis)

        for axis in axes:
            if not 0 <= axis <= self.ndim:
                raise ValueError(f'Invalid axis {axis}. Axis must be between 0 and {self.ndim}.')

        if len(set(axes)) != len(axes):
            raise ValueError(f'Axes should not be repeated: {axes}')

        return construct_expr(NDArrayAgg(self._ir, axes),
                              tndarray(self._type.element_type, self.ndim - len(axes)),
                              self._indices,
                              self._aggregations)

    @typecheck_method(uri=str)
    def save(self, uri):
        """Write out the NDArray to the given path as in .npy format. If the URI does not
        end with ".npy" the file extension will be appended. This method reflects the numpy
        `save` method. NDArrays saved with this method can be loaded into numpy using numpy
        `load`.

        Examples
        --------
        >>> import numpy as np
        >>> nd.save('file://local/file') # doctest: +SKIP
        >>> np.load('/local/file.npy') # doctest: +SKIP
        array([[1, 2],
               [3, 4]], dtype=int32)

        Parameters
        ----------
        uri : :obj: `str`
        """
        if not uri.endswith('.npy'):
            uri += '.npy'

        Env.backend().execute(NDArrayWrite(self._ir, hl.str(uri)._ir))

    def to_numpy(self):
        """Execute and convert this NDArray to a `NumPy` ndarray.

        Examples
        --------
        >>> a = nd.to_numpy() # doctest: +SKIP

        Returns
        -------
        :class:`numpy.ndarray`
        """
        # FIXME Use filesystem abstraction instead when that is ready
        temp_file = tempfile.NamedTemporaryFile(suffix='.npy').name
        self.save(temp_file)
        return np.load(temp_file)


scalars = {tbool: BooleanExpression,
           tint32: Int32Expression,
           tint64: Int64Expression,
           tfloat32: Float32Expression,
           tfloat64: Float64Expression,
           tstr: StringExpression,
           tcall: CallExpression}

typ_to_expr = {
    tlocus: LocusExpression,
    tinterval: IntervalExpression,
    tcall: CallExpression,
    tdict: DictExpression,
    tarray: ArrayExpression,
    tset: SetExpression,
    tstruct: StructExpression,
    ttuple: TupleExpression,
    tndarray: NDArrayExpression
}

def apply_expr(f, result_type, *args):
    indices, aggregations = unify_all(*args)
    ir = f(*[arg._ir for arg in args])
    return construct_expr(ir, result_type, indices, aggregations)

@typecheck(ir=IR, type=nullable(HailType), indices=Indices, aggregations=LinkedList)
def construct_expr(ir: IR,
                   type: HailType,
                   indices: Indices = Indices(),
                   aggregations: LinkedList = LinkedList(Aggregation)):
    if type is None:
        return Expression(ir, None, indices, aggregations)
    elif isinstance(type, tarray) and is_numeric(type.element_type):
        return ArrayNumericExpression(ir, type, indices, aggregations)
    elif isinstance(type, tarray):
        etype = type.element_type
        if isinstance(etype, (hl.tarray, hl.tset)):
            while isinstance(etype, (hl.tarray, hl.tset)):
                etype = etype.element_type
        if isinstance(etype, hl.tstruct):
            return ArrayStructExpression(ir, type, indices, aggregations)
        else:
            return typ_to_expr[type.__class__](ir, type, indices, aggregations)
    elif isinstance(type, tset):
        etype = type.element_type
        if isinstance(etype, (hl.tarray, hl.tset)):
            while isinstance(etype, (hl.tarray, hl.tset)):
                etype = etype.element_type
        if isinstance(etype, hl.tstruct):
            return SetStructExpression(ir, type, indices, aggregations)
        else:
            return typ_to_expr[type.__class__](ir, type, indices, aggregations)
    elif isinstance(type, tndarray) and is_numeric(type.element_type):
        return NDArrayNumericExpression(ir, type, indices, aggregations)
    elif type in scalars:
        return scalars[type](ir, type, indices, aggregations)
    elif type.__class__ in typ_to_expr:
        return typ_to_expr[type.__class__](ir, type, indices, aggregations)
    else:
        raise NotImplementedError(type)


@typecheck(name=str, type=HailType, indices=Indices)
def construct_reference(name, type, indices):
    assert isinstance(type, hl.tstruct)
    ir = SelectFields(TopLevelReference(name), list(type))
    return construct_expr(ir, type, indices)

@typecheck(name=str, type=HailType, indices=Indices, aggregations=LinkedList)
def construct_variable(name, type,
                       indices: Indices = Indices(),
                       aggregations: LinkedList = LinkedList(Aggregation)):
    return construct_expr(Ref(name), type, indices, aggregations)
