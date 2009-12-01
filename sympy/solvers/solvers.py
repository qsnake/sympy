﻿""" This module contain solvers for all kinds of equations:

    - algebraic, use solve()

    - recurrence, use rsolve()

    - differential, use dsolve()

    - transcendental, use tsolve()

    - nonlinear (numerically), use nsolve()
      (you will need a good starting point)

"""

from sympy.core.sympify import sympify
from sympy.core.basic import S, Mul
from sympy.core.add import Add
from sympy.core.power import Pow
from sympy.core.symbol import Symbol, Wild
from sympy.core.relational import Equality
from sympy.core.numbers import ilcm

from sympy.functions import sqrt, log, exp, LambertW
from sympy.simplify import simplify, collect
from sympy.matrices import Matrix, zeros
from sympy.polys import roots
from sympy.functions.elementary.piecewise import Piecewise, piecewise_fold

from sympy.utilities import any, all
from sympy.utilities.iterables import iff
from sympy.utilities.lambdify import lambdify
from sympy.mpmath import findroot

from sympy.solvers.polysys import solve_poly_system

from warnings import warn

# Codes for guess solve strategy
GS_POLY = 0
GS_RATIONAL = 1
GS_POLY_CV_1 = 2 # can be converted to a polynomial equation via the change of variable y -> x**a, a real
GS_POLY_CV_2 = 3 # can be converted to a polynomial equation multiplying on both sides by x**m
                 # for example, x + 1/x == 0. Multiplying by x yields x**2 + x == 0
GS_RATIONAL_CV_1 = 4 # can be converted to a rational equation via the change of variable y -> x**n
GS_PIECEWISE = 5
GS_TRANSCENDENTAL = 6

def guess_solve_strategy(expr, symbol):
    """
    Tries to guess what approach should be used to solve a specific equation

    Returns
    =======
       - -1: could not guess
       - integer > 0: code representing certain type of equation. See GS_* fields
         on this module for a complete list

    Examples
    ========
    >>> from sympy import Symbol, Rational
    >>> x = Symbol('x')
    >>> guess_solve_strategy(x**2 + 1, x)
    0
    >>> guess_solve_strategy(x**Rational(1,2) + 1, x)
    2

    """
    eq_type = -1
    if expr.is_Add:
        return max([guess_solve_strategy(i, symbol) for i in expr.args])

    elif expr.is_Mul:
        # check for rational functions
        num, denom = expr.as_numer_denom()
        if denom != 1 and denom.has(symbol):
            #we have a quotient
            m = max(guess_solve_strategy(num, symbol), guess_solve_strategy(denom, symbol))
            if m == GS_POLY:
                return GS_RATIONAL
            elif m == GS_POLY_CV_1:
                return GS_RATIONAL_CV_1
            else:
                raise NotImplementedError
        else:
            return max([guess_solve_strategy(i, symbol) for i in expr.args])

    elif expr.is_Symbol:
        return GS_POLY

    elif expr.is_Pow:
        if expr.exp.has(symbol):
            return GS_TRANSCENDENTAL
        elif not expr.exp.has(symbol) and expr.base.has(symbol):
            if expr.exp.is_Integer and expr.exp > 0:
                eq_type = max(eq_type, GS_POLY)
            elif expr.exp.is_Integer and expr.exp < 0:
                eq_type = max(eq_type, GS_POLY_CV_2)
            elif expr.exp.is_Rational:
                eq_type = max(eq_type, GS_POLY_CV_1)
            else:
                return GS_TRANSCENDENTAL

    elif expr.is_Piecewise:
        return GS_PIECEWISE

    elif expr.is_Function and expr.has(symbol):
        return GS_TRANSCENDENTAL

    elif not expr.has(symbol):
        return GS_POLY

    return eq_type

def solve(f, *symbols, **flags):
    """Solves equations and systems of equations.

       Currently supported are univariate polynomial, transcendental
       equations, piecewise combinations thereof and systems of linear
       and polynomial equations.  Input is formed as a single expression
       or an equation,  or an iterable container in case of an equation
       system.  The type of output may vary and depends heavily on the
       input. For more details refer to more problem specific functions.

       By default all solutions are simplified to make the output more
       readable. If this is not the expected behavior (e.g., because of
       speed issues) set simplified=False in function arguments.

       To solve equations and systems of equations like recurrence relations
       or differential equations, use rsolve() or dsolve(), respectively.

       >>> from sympy import *
       >>> x,y = symbols('xy')

       Solve a polynomial equation:

       >>> solve(x**4-1, x)
       [1, -1, -I, I]

       Solve a linear system:

       >>> solve((x+5*y-2, -3*x+6*y-15), x, y)
       {x: -3, y: 1}

    """
    def sympit(w):
        return map(sympify, iff(isinstance(w,(list, tuple, set)), w, [w]))
    # make f and symbols into lists of sympified quantities
    # keeping track of how f was passed since if it is a list
    # a dictionary of results will be returned.
    bare_f = not isinstance(f, (list, tuple, set))
    f, symbols = (sympit(w) for w in [f, symbols])

    for i, fi in enumerate(f):
        if isinstance(fi, Equality):
            f[i] = fi.lhs - fi.rhs

    if not symbols:
        #get symbols from equations or supply dummy symbols since
        #solve(3,x) returns []...though it seems that it should raise some sort of error TODO
        symbols = set([])
        for fi in f:
            symbols |= fi.atoms(Symbol) or set([Symbol('x',dummy=True)])
        symbols = list(symbols)

    if bare_f:
        f=f[0]
    if len(symbols) == 1:
        if isinstance(symbols[0], (list, tuple, set)):
            symbols = symbols[0]

    result = list()

    # Begin code handling for Function and Derivative instances
    # Basic idea:  store all the passed symbols in symbols_passed, check to see
    # if any of them are Function or Derivative types, if so, use a dummy
    # symbol in their place, and set symbol_swapped = True so that other parts
    # of the code can be aware of the swap.  Once all swapping is done, the
    # continue on with regular solving as usual, and swap back at the end of
    # the routine, so that whatever was passed in symbols is what is returned.
    symbols_new = []
    symbol_swapped = False

    symbols_passed = list(symbols)

    for i, s in enumerate(symbols):
        if s.is_Symbol:
            s_new = s
        elif s.is_Function:
            symbol_swapped = True
            s_new = Symbol('F%d' % i, dummy=True)
        elif s.is_Derivative:
            symbol_swapped = True
            s_new = Symbol('D%d' % i, dummy=True)
        else:
            raise TypeError('not a Symbol or a Function')
        symbols_new.append(s_new)

        if symbol_swapped:
            swap_back_dict = dict(zip(symbols_new, symbols))
    # End code for handling of Function and Derivative instances

    if not isinstance(f, (tuple, list, set)):

        # Create a swap dictionary for storing the passed symbols to be solved
        # for, so that they may be swapped back.
        if symbol_swapped:
            swap_dict = zip(symbols, symbols_new)
            f = f.subs(swap_dict)
            symbols = symbols_new

        # Any embedded piecewise functions need to be brought out to the
        # top level so that the appropriate strategy gets selected.
        f = piecewise_fold(f)

        if len(symbols) != 1:
            result = {}
            for s in symbols:
                result[s] = solve(f, s, **flags)
            if flags.get('simplified', True):
                for s, r in result.items():
                    result[s] = map(simplify, r)
            return result

        symbol = symbols[0]
        strategy = guess_solve_strategy(f, symbol)

        if strategy == GS_POLY:
            poly = f.as_poly( symbol )
            if poly is None:
                raise NotImplementedError("Cannot solve equation " + str(f) + " for "
                    + str(symbol))
            # for cubics and quartics, if the flag wasn't set, DON'T do it
            # by default since the results are quite long. Perhaps one could
            # base this decision on a certain crtical length of the roots.
            if poly.degree > 2:
                flags['simplified'] = flags.get('simplified', False)
            result = roots(poly, cubics=True, quartics=True).keys()

        elif strategy == GS_RATIONAL:
            P, Q = f.as_numer_denom()
            #TODO: check for Q != 0
            result = solve(P, symbol, **flags)

        elif strategy == GS_POLY_CV_1:
            args = list(f.args)
            if isinstance(f, Add):
                # we must search for a suitable change of variable
                # collect exponents
                exponents_denom = list()
                for arg in args:
                    if isinstance(arg, Pow):
                        exponents_denom.append(arg.exp.q)
                    elif isinstance(arg, Mul):
                        for mul_arg in arg.args:
                            if isinstance(mul_arg, Pow):
                                exponents_denom.append(mul_arg.exp.q)
                assert len(exponents_denom) > 0
                if len(exponents_denom) == 1:
                    m = exponents_denom[0]
                else:
                    # get the GCD of the denominators
                    m = reduce(ilcm, exponents_denom)
                # x -> y**m.
                # we assume positive for simplification purposes
                t = Symbol('t', positive=True, dummy=True)
                f_ = f.subs(symbol, t**m)
                if guess_solve_strategy(f_, t) != GS_POLY:
                    raise NotImplementedError("Could not convert to a polynomial equation: %s" % f_)
                cv_sols = solve(f_, t)
                for sol in cv_sols:
                    result.append(sol**m)

            elif isinstance(f, Mul):
                for mul_arg in args:
                    result.extend(solve(mul_arg, symbol))

        elif strategy == GS_POLY_CV_2:
            m = 0
            args = list(f.args)
            if isinstance(f, Add):
                for arg in args:
                    if isinstance(arg, Pow):
                        m = min(m, arg.exp)
                    elif isinstance(arg, Mul):
                        for mul_arg in arg.args:
                            if isinstance(mul_arg, Pow):
                                m = min(m, mul_arg.exp)
            elif isinstance(f, Mul):
                for mul_arg in args:
                    if isinstance(mul_arg, Pow):
                        m = min(m, mul_arg.exp)
            f1 = simplify(f*symbol**(-m))
            result = solve(f1, symbol)
            # TODO: we might have introduced unwanted solutions
            # when multiplied by x**-m

        elif strategy == GS_PIECEWISE:
            result = set()
            for expr, cond in f.args:
                candidates = solve(expr, *symbols)
                if isinstance(cond, bool) or cond.is_Number:
                    if not cond:
                        continue

                    # Only include solutions that do not match the condition
                    # of any of the other pieces.
                    for candidate in candidates:
                        matches_other_piece = False
                        for other_expr, other_cond in f.args:
                            if isinstance(other_cond, bool) \
                               or other_cond.is_Number:
                                continue
                            if bool(other_cond.subs(symbol, candidate)):
                                matches_other_piece = True
                                break
                        if not matches_other_piece:
                            result.add(candidate)
                else:
                    for candidate in candidates:
                        if bool(cond.subs(symbol, candidate)):
                            result.add(candidate)

            result = list(result)

        elif strategy == GS_TRANSCENDENTAL:
            #a, b = f.as_numer_denom()
            # Let's throw away the denominator for now. When we have robust
            # assumptions, it should be checked, that for the solution,
            # b!=0.
            result = tsolve(f, *symbols)
        elif strategy == -1:
            raise ValueError('Could not parse expression %s' % f)
        else:
            raise NotImplementedError("No algorithms are implemented to solve equation %s" % f)

        # This symbol swap should not be necessary for the single symbol case: if you've
        # solved for the symbol the it will not appear in the solution. Right now, however
        # ode's are getting solutions for solve (even though they shouldn't be -- see the
        # swap_back test in test_solvers).
        if symbol_swapped:
            result = [ri.subs(swap_back_dict) for ri in result]

        if flags.get('simplified', True) and strategy != GS_RATIONAL:
            return map(simplify, result)
        else:
            return result
    else:
        if not f:
            return {}
        else:
            # Create a swap dictionary for storing the passed symbols to be
            # solved for, so that they may be swapped back.
            if symbol_swapped:
                swap_dict = zip(symbols, symbols_new)
                f = [fi.subs(swap_dict) for fi in f]
                symbols = symbols_new

            polys = []

            for g in f:

                poly = g.as_poly(*symbols)

                if poly is not None:
                    polys.append(poly)
                else:
                    raise NotImplementedError()

            if all(p.is_linear for p in polys):
                n, m = len(f), len(symbols)
                matrix = zeros((n, m + 1))

                for i, poly in enumerate(polys):
                    for coeff, monom in poly.iter_terms():
                        try:
                            j = list(monom).index(1)
                            matrix[i, j] = coeff
                        except ValueError:
                            matrix[i, m] = -coeff

                soln = solve_linear_system(matrix, *symbols, **flags)
            else:
                soln = solve_poly_system(polys)

            # Use swap_dict to ensure we return the same type as what was
            # passed
            if symbol_swapped:
                if isinstance(soln, dict):
                    res = {}
                    for k in soln.keys():
                        res.update({swap_back_dict[k]: soln[k]})
                    return res
                else:
                    return soln
            else:
                return soln

def solve_linear_system(system, *symbols, **flags):
    """Solve system of N linear equations with M variables, which means
       both Cramer and over defined systems are supported. The possible
       number of solutions is zero, one or infinite. Respectively this
       procedure will return None or dictionary with solutions. In the
       case of over defined system all arbitrary parameters are skipped.
       This may cause situation in with empty dictionary is returned.
       In this case it means all symbols can be assigned arbitrary values.

       Input to this functions is a Nx(M+1) matrix, which means it has
       to be in augmented form. If you are unhappy with such setting
       use 'solve' method instead, where you can input equations
       explicitly. And don't worry about the matrix, this function
       is persistent and will make a local copy of it.

       The algorithm used here is fraction free Gaussian elimination,
       which results, after elimination, in upper-triangular matrix.
       Then solutions are found using back-substitution. This approach
       is more efficient and compact than the Gauss-Jordan method.

       >>> from sympy import *
       >>> x, y = symbols('xy')

       Solve the following system:

              x + 4 y ==  2
           -2 x +   y == 14

       >>> system = Matrix(( (1, 4, 2), (-2, 1, 14)))
       >>> solve_linear_system(system, x, y)
       {x: -6, y: 2}

    """
    matrix = system[:,:]
    syms = list(symbols)

    i, m = 0, matrix.cols-1  # don't count augmentation

    while i < matrix.rows:
        if i == m:
            # an overdetermined system
            if any(matrix[i:,m]):
                return None   # no solutions
            else:
                # remove trailing rows
                matrix = matrix[:i,:]
                break

        if not matrix[i, i]:
            # there is no pivot in current column
            # so try to find one in other columns
            for k in xrange(i+1, m):
                if matrix[i, k]:
                    break
            else:
                if matrix[i, m]:
                    return None   # no solutions
                else:
                    # zero row or was a linear combination of
                    # other rows so now we can safely skip it
                    matrix.row_del(i)
                    continue

            # we want to change the order of colums so
            # the order of variables must also change
            syms[i], syms[k] = syms[k], syms[i]
            matrix.col_swap(i, k)

        pivot_inv = S.One / matrix [i, i]

        # divide all elements in the current row by the pivot
        matrix.row(i, lambda x, _: x * pivot_inv)

        for k in xrange(i+1, matrix.rows):
            if matrix[k, i]:
                coeff = matrix[k, i]

                # subtract from the current row the row containing
                # pivot and multiplied by extracted coefficient
                matrix.row(k, lambda x, j: simplify(x - matrix[i, j]*coeff))

        i += 1

    # if there weren't any problems, augmented matrix is now
    # in row-echelon form so we can check how many solutions
    # there are and extract them using back substitution

    simplified = flags.get('simplified', True)

    if len(syms) == matrix.rows:
        # this system is Cramer equivalent so there is
        # exactly one solution to this system of equations
        k, solutions = i-1, {}

        while k >= 0:
            content = matrix[k, m]

            # run back-substitution for variables
            for j in xrange(k+1, m):
                content -= matrix[k, j]*solutions[syms[j]]

            if simplified:
                solutions[syms[k]] = simplify(content)
            else:
                solutions[syms[k]] = content

            k -= 1

        return solutions
    elif len(syms) > matrix.rows:
        # this system will have infinite number of solutions
        # dependent on exactly len(syms) - i parameters
        k, solutions = i-1, {}

        while k >= 0:
            content = matrix[k, m]

            # run back-substitution for variables
            for j in xrange(k+1, i):
                content -= matrix[k, j]*solutions[syms[j]]

            # run back-substitution for parameters
            for j in xrange(i, m):
                content -= matrix[k, j]*syms[j]

            if simplified:
                solutions[syms[k]] = simplify(content)
            else:
                solutions[syms[k]] = content

            k -= 1

        return solutions
    else:
        return None   # no solutions

def solve_undetermined_coeffs(equ, coeffs, sym, **flags):
    """Solve equation of a type p(x; a_1, ..., a_k) == q(x) where both
       p, q are univariate polynomials and f depends on k parameters.
       The result of this functions is a dictionary with symbolic
       values of those parameters with respect to coefficients in q.

       This functions accepts both Equations class instances and ordinary
       SymPy expressions. Specification of parameters and variable is
       obligatory for efficiency and simplicity reason.

       >>> from sympy import *
       >>> a, b, c, x = symbols('a', 'b', 'c', 'x')

       >>> solve_undetermined_coeffs(Eq(2*a*x + a+b, x), [a, b], x)
       {a: 1/2, b: -1/2}

       >>> solve_undetermined_coeffs(Eq(a*c*x + a+b, x), [a, b], x)
       {a: 1/c, b: -1/c}

    """
    if isinstance(equ, Equality):
        # got equation, so move all the
        # terms to the left hand side
        equ = equ.lhs - equ.rhs

    system = collect(equ.expand(), sym, evaluate=False).values()

    if not any([ equ.has(sym) for equ in system ]):
        # consecutive powers in the input expressions have
        # been successfully collected, so solve remaining
        # system using Gaussian elimination algorithm
        return solve(system, *coeffs, **flags)
    else:
        return None # no solutions

def solve_linear_system_LU(matrix, syms):
    """ LU function works for invertible only """
    assert matrix.rows == matrix.cols-1
    A = matrix[:matrix.rows,:matrix.rows]
    b = matrix[:,matrix.cols-1:]
    soln = A.LUsolve(b)
    solutions = {}
    for i in range(soln.rows):
        solutions[syms[i]] = soln[i,0]
    return solutions

x = Symbol('x', dummy=True)
a,b,c,d,e,f,g,h = [Wild(t, exclude=[x]) for t in 'abcdefgh']
patterns = None

def _generate_patterns():
    """
    Generates patterns for transcendental equations.

    This is lazily calculated (called) in the tsolve() function and stored in
    the patterns global variable.
    """

    tmp1 = f ** (h-(c*g/b))
    tmp2 = (-e*tmp1/a)**(1/d)
    global patterns
    patterns = [
        (a*(b*x+c)**d + e   , ((-(e/a))**(1/d)-c)/b),
        (    b+c*exp(d*x+e) , (log(-b/c)-e)/d),
        (a*x+b+c*exp(d*x+e) , -b/a-LambertW(c*d*exp(e-b*d/a)/a)/d),
        (    b+c*f**(d*x+e) , (log(-b/c)-e*log(f))/d/log(f)),
        (a*x+b+c*f**(d*x+e) , -b/a-LambertW(c*d*f**(e-b*d/a)*log(f)/a)/d/log(f)),
        (    b+c*log(d*x+e) , (exp(-b/c)-e)/d),
        (a*x+b+c*log(d*x+e) , -e/d+c/a*LambertW(a/c/d*exp(-b/c+a*e/c/d))),
        (a*(b*x+c)**d + e*f**(g*x+h) , -c/b-d*LambertW(-tmp2*g*log(f)/b/d)/g/log(f))
    ]

def tsolve(eq, sym):
    """
    Solves a transcendental equation with respect to the given
    symbol. Various equations containing mixed linear terms, powers,
    and logarithms, can be solved.

    Only a single solution is returned. This solution is generally
    not unique. In some cases, a complex solution may be returned
    even though a real solution exists.

        >>> from sympy import *
        >>> x = Symbol('x')

        >>> tsolve(3**(2*x+5)-4, x)
        [(-5*log(3) + log(4))/(2*log(3))]

        >>> tsolve(log(x) + 2*x, x)
        [LambertW(2)/2]

    """
    if patterns is None:
        _generate_patterns()
    eq = sympify(eq)
    if isinstance(eq, Equality):
        eq = eq.lhs - eq.rhs
    sym = sympify(sym)
    eq2 = eq.subs(sym, x)
    # First see if the equation has a linear factor
    # In that case, the other factor can contain x in any way (as long as it
    # is finite), and we have a direct solution to which we add others that
    # may be found for the remaining portion.
    r = Wild('r')
    m = eq2.match((a*x+b)*r)
    if m and m[a]:
        return [(-b/a).subs(m).subs(x, sym)] + solve(m[r], x)
    for p, sol in patterns:
        m = eq2.match(p)
        if m:
            return [sol.subs(m).subs(x, sym)]

    # let's also try to inverse the equation
    lhs = eq
    rhs = S.Zero

    while True:
        indep, dep = lhs.as_independent(sym)

        # dep + indep == rhs
        if lhs.is_Add:
            # this indicates we have done it all
            if indep is S.Zero:
                break

            lhs = dep
            rhs-= indep

        # dep * indep == rhs
        else:
            # this indicates we have done it all
            if indep is S.One:
                break

            lhs = dep
            rhs/= indep

    #                    -1
    # f(x) = g  ->  x = f  (g)
    if lhs.is_Function and lhs.nargs==1 and hasattr(lhs, 'inverse'):
        rhs = lhs.inverse() (rhs)
        lhs = lhs.args[0]

        sol = solve(lhs-rhs, sym)
        return sol

    elif lhs.is_Add:
        # just a simple case - we do variable substitution for first function,
        # and if it removes all functions - let's call solve.
        #      x    -x                   -1
        # UC: e  + e   = y      ->  t + t   = y
        t = Symbol('t', dummy=True)
        terms = lhs.args

        # find first term which is Function
        for f1 in lhs.args:
            if f1.is_Function:
                break
        else:
            raise NotImplementedError("Unable to solve the equation" + \
                "(tsolve: at least one Function expected at this point")

        # perform the substitution
        lhs_ = lhs.subs(f1, t)

        # if no Functions left, we can proceed with usual solve
        if not (lhs_.is_Function or
                any(term.is_Function for term in lhs_.args)):
            cv_sols = solve(lhs_ - rhs, t)
            for sol in cv_sols:
                if sol.has(sym):
                    raise NotImplementedError("Unable to solve the equation")
            cv_inv = solve( t - f1, sym )[0]
            sols = list()
            for sol in cv_sols:
                sols.append(cv_inv.subs(t, sol))
            return sols


    raise NotImplementedError("Unable to solve the equation.")

def msolve(*args, **kwargs):
    """
    Compatibility wrapper pointing to nsolve().

    msolve() has been renamed to nsolve(), please use nsolve() directly."""
    warn('msolve() is has been renamed, please use nsolve() instead',
         DeprecationWarning)
    args[0], args[1] = args[1], args[0]
    return nsolve(*args, **kwargs)

# TODO: option for calculating J numerically
def nsolve(*args, **kwargs):
    """
    Solve a nonlinear equation system numerically.

    nsolve(f, [args,] x0, modules=['mpmath'], **kwargs)

    f is a vector function of symbolic expressions representing the system.
    args are the variables. If there is only one variable, this argument can be
    omitted.
    x0 is a starting vector close to a solution.

    Use the modules keyword to specify which modules should be used to evaluate
    the function and the Jacobian matrix. Make sure to use a module that
    supports matrices. For more information on the syntax, please see the
    docstring of lambdify.

    Overdetermined systems are supported.

    >>> from sympy import Symbol, nsolve
    >>> import sympy
    >>> sympy.mpmath.mp.dps = 15
    >>> x1 = Symbol('x1')
    >>> x2 = Symbol('x2')
    >>> f1 = 3 * x1**2 - 2 * x2**2 - 1
    >>> f2 = x1**2 - 2 * x1 + x2**2 + 2 * x2 - 8
    >>> print nsolve((f1, f2), (x1, x2), (-1, 1))
    [-1.19287309935246]
    [ 1.27844411169911]

    For one-dimensional functions the syntax is simplified:

    >>> from sympy import sin
    >>> nsolve(sin(x), x, 2)
    3.14159265358979
    >>> nsolve(sin(x), 2)
    3.14159265358979

    mpmath.findroot is used, you can find there more extensive documentation,
    especially concerning keyword parameters and available solvers.
    """
    # interpret arguments
    if len(args) == 3:
        f = args[0]
        fargs = args[1]
        x0 = args[2]
    elif len(args) == 2:
        f = args[0]
        fargs = None
        x0 = args[1]
    elif len(args) < 2:
        raise TypeError('nsolve expected at least 2 arguments, got %i'
                        % len(args))
    else:
        raise TypeError('nsolve expected at most 3 arguments, got %i'
                        % len(args))
    modules = kwargs.get('modules', ['mpmath'])
    if isinstance(f,  (list,  tuple)):
        f = Matrix(f).T
    if not isinstance(f, Matrix):
        # assume it's a sympy expression
        if isinstance(f, Equality):
            f = f.lhs - f.rhs
        f = f.evalf()
        atoms = f.atoms(Symbol)
        if fargs is None:
            fargs = atoms.copy().pop()
        if not (len(atoms) == 1 and (fargs in atoms or fargs[0] in atoms)):
            raise ValueError('expected a one-dimensional and numerical function')

        # the function is much better behaved if there is no denominator
        f = f.as_numer_denom()[0]

        f = lambdify(fargs, f, modules)
        return findroot(f, x0, **kwargs)
    if len(fargs) > f.cols:
        raise NotImplementedError('need at least as many equations as variables')
    verbose = kwargs.get('verbose', False)
    if verbose:
        print 'f(x):'
        print f
    # derive Jacobian
    J = f.jacobian(fargs)
    if verbose:
        print 'J(x):'
        print J
    # create functions
    f = lambdify(fargs, f.T, modules)
    J = lambdify(fargs, J, modules)
    # solve the system numerically
    x = findroot(f, x0, J=J, **kwargs)
    return x
