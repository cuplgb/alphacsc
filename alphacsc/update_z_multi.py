"""Convolutional dictionary learning"""

# Authors: Mainak Jas <mainak.jas@telecom-paristech.fr>
#          Tom Dupre La Tour <tom.duprelatour@telecom-paristech.fr>
#          Umut Simsekli <umut.simsekli@telecom-paristech.fr>
#          Alexandre Gramfort <alexandre.gramfort@inria.fr>
#          Thomas Moreau <thomas.moreau@inria.fr>

import numpy as np
from scipy import optimize
from joblib import Parallel, delayed

from .utils.optim import power_iteration
from .loss_and_gradient import gradient_zi
from .utils.compute_constants import compute_DtD
from .utils.convolution import _choose_convolve_multi


def update_z_multi(X, D, reg, z0=None, debug=False, parallel=None,
                   solver='l_bfgs', solver_kwargs=dict(), loss='l2',
                   loss_params=dict(), freeze_support=False):
    """Update Z using L-BFGS with positivity constraints

    Parameters
    ----------
    X : array, shape (n_trials, n_channels, n_times)
        The data array
    D : array, shape (n_atoms, n_channels + n_times_atom)
        The dictionary used to encode the signal X. Can be either in the form
        f a full rank dictionary D (n_atoms, n_channels, n_times_atom) or with
        the spatial and temporal atoms uv (n_atoms, n_channels + n_times_atom).
    reg : float
        The regularization constant
    z0 : None | array, shape (n_atoms, n_trials, n_times_valid)
        Init for z (can be used for warm restart).
    debug : bool
        If True, check the grad.
    parallel : instance of Parallel
        Context manager for running joblibs in a loop.
    solver : 'l_bfgs' | 'gcd'
        The solver to use.
    solver_kwargs : dict
        Parameters for the solver
    loss : 'l2' | 'dtw' | 'whitening'
        The data fit loss, either classical l2 norm or the soft-DTW loss.
    loss_params : dict
        Parameters of the loss
    freeze_support : boolean
        If True, the support of z0 is frozen.

    Returns
    -------
    z : array, shape (n_atoms, n_trials, n_times - n_times_atom + 1)
        The true codes.
    """
    n_trials, n_channels, n_times = X.shape
    if D.ndim == 2:
        n_atoms, n_channels_n_times_atom = D.shape
        n_times_atom = n_channels_n_times_atom - n_channels
    else:
        n_atoms, n_channels, n_times_atom = D.shape
    n_times_valid = n_times - n_times_atom + 1

    # now estimate the codes
    my_update_z = delayed(_update_z_multi_idx)
    if parallel is None:
        parallel = Parallel(n_jobs=1)

    zhats = parallel(
        my_update_z(X, D, reg, z0, i, debug, solver, solver_kwargs,
                    freeze_support, loss, loss_params=loss_params)
        for i in np.array_split(np.arange(n_trials), parallel.n_jobs))
    z_hat = np.vstack(zhats)

    z_hat2 = z_hat.reshape((n_trials, n_atoms, n_times_valid))
    z_hat2 = np.swapaxes(z_hat2, 0, 1)

    return z_hat2


def _update_z_multi_idx(X, D, reg, z0, idxs, debug, solver="l_bfgs",
                        solver_kwargs=dict(), freeze_support=False, loss='l2',
                        loss_params=dict()):
    n_trials, n_channels, n_times = X.shape
    if D.ndim == 2:
        n_atoms, n_channels_n_times_atom = D.shape
        n_times_atom = n_channels_n_times_atom - n_channels
    else:
        n_atoms, n_channels, n_times_atom = D.shape
    n_times_valid = n_times - n_times_atom + 1

    assert not (freeze_support and z0 is None), 'Impossible !'

    constants = {}
    zhats = []

    if solver == "gcd":
        constants['DtD'] = compute_DtD(D=D, n_channels=n_channels)

    for i in idxs:

        def func_and_grad(zi):
            return gradient_zi(Xi=X[i], zi=zi, D=D, constants=constants,
                               reg=reg, return_func=True, flatten=True,
                               loss=loss, loss_params=loss_params)

        def grad_noreg(zi):
            return gradient_zi(Xi=X[i], zi=zi, D=D, constants=constants,
                               reg=None, return_func=False, flatten=True,
                               loss=loss, loss_params=loss_params)

        if z0 is None:
            f0 = np.zeros(n_atoms * n_times_valid)
        else:
            f0 = z0[:, i, :].reshape(n_atoms * n_times_valid)

        if freeze_support:
            bounds = [(0, 0) if z == 0 else (0, None) for z in f0]
        else:
            bounds = [(0, None) for z in f0]

        if debug:

            def pobj(zi):
                return func_and_grad(zi)[0]

            def fprime(zi):
                return func_and_grad(zi)[1]

            try:
                assert optimize.check_grad(pobj, fprime, f0) < 1e-2
            except AssertionError:
                grad_approx = optimize.approx_fprime(f0, pobj, 2e-8)
                grad_z = fprime(f0)

                import matplotlib.pyplot as plt
                plt.semilogy(abs(grad_approx - grad_z))
                plt.figure()
                plt.plot(grad_approx, label="approx")
                plt.plot(grad_z, '--', label="grad")
                plt.legend()
                plt.show()

                raise

        if solver == "l_bfgs":
            factr = solver_kwargs.get('factr', 1e15)  # default value
            zhat, f, d = optimize.fmin_l_bfgs_b(func_and_grad, f0, fprime=None,
                                                args=(), approx_grad=False,
                                                bounds=bounds, factr=factr,
                                                maxiter=1e6)
        elif solver == "ista":
            raise NotImplementedError('Not adapted yet for n_channels')
        elif solver == "fista":
            raise NotImplementedError('Not adapted yet for n_channels')
        elif solver == "gcd":
            f0 = f0.reshape(n_atoms, n_times_valid)
            # Default values
            tol = solver_kwargs.get('tol', 1e-1)
            n_seg = solver_kwargs.get('n_seg', 'auto')
            max_iter = solver_kwargs.get('max_iter', 1e15)
            strategy = solver_kwargs.get('strategy', 'greedy')
            zhat = _coordinate_descent_idx(X[i], D, constants, reg=reg, z0=f0,
                                           freeze_support=freeze_support,
                                           tol=tol, max_iter=max_iter,
                                           n_seg=n_seg, strategy=strategy)
            # raise NotImplementedError('Not implemented yet!')
        else:
            raise ValueError("Unrecognized solver %s. Must be 'ista', 'fista',"
                             " or 'l_bfgs'." % solver)

        zhats.append(zhat)
    return np.vstack(zhats)


def _coordinate_descent_idx(Xi, D, constants, reg, z0=None, max_iter=1000,
                            tol=1e-1, strategy='greedy', n_seg='auto',
                            freeze_support=False, debug=False, verbose=0):
    """Compute the coding signal associated to Xi with coordinate descent.

    Parameters
    ----------
    Xi : array, shape (n_channels, n_times)
        The signal to encode.
    D : array
        The atoms. Can either be full rank with shape shape
        (n_atoms, n_channels, n_times_atom) or rank 1 with
        shape shape (n_atoms, n_channels + n_times_atom)
    constants : dict
        Constants containing DtD to speedup computation
    z0 : array, shape (n_atoms, n_time_valid)
        Initial estimate of the coding signal, to warm start the algorithm.
    tol : float
        Tolerance for the stopping criterion of the algorithm
    max_iter : int
        Maximal number of iterations run by the algorithm
    strategy : str in {'greedy' | 'random'}
        Strategy to select the updated coordinate in the CD algorithm.
    n_seg : int or 'auto'
        Number of segments used to divide the coding signal. The updates are
        performed successively on each of these segments.
    freeze_support : boolean
        If set to True, only update the coefficient that are non-zero in z0.
    """
    n_channels, n_times = Xi.shape
    if D.ndim == 2:
        n_atoms, n_times_atom = D.shape
        n_times_atom -= n_channels
    else:
        n_atoms, n_channels, n_times_atom = D.shape
    n_times_valid = n_times - n_times_atom + 1
    t0 = n_times_atom - 1

    if z0 is None:
        z_hat = np.zeros((n_atoms, n_times_valid))
    else:
        z_hat = z0.copy()

    if n_seg == 'auto':
        n_seg = max(n_times_valid // (2 * n_times_atom), 1)
        # n_s    eg = max(n_times_valid // n_times_atom, 1)

    max_iter *= n_seg

    n_times_seg = n_times_valid // n_seg + 1

    def objective(zi):
        Dzi = _choose_convolve_multi(zi, D=D, n_channels=n_channels)
        Dzi -= Xi
        func = 0.5 * np.dot(Dzi.ravel(), Dzi.ravel())
        func += reg * zi.sum()
        return func

    DtD = constants["DtD"]
    norm_Dk = np.array([DtD[k, k, t0] for k in range(n_atoms)])[:, None]
    if debug:
        pobj = [objective(z_hat)]

    # Init beta with -DtX
    # beta = _fprime(uv, z_hat.ravel(), Xi=Xi, reg=None, return_func=False)
    # beta = beta.reshape(n_atoms, n_times_valid)
    beta = gradient_zi(Xi, z_hat, D=D, reg=None, loss='l2',
                       return_func=False, constants=constants)
    for k, t in zip(*z_hat.nonzero()):
        beta[k, t] -= z_hat[k, t] * norm_Dk[k]  # np.sum(DtD[k, k, t0])
    dz_opt = np.maximum(-beta - reg, 0) / norm_Dk - z_hat
    if freeze_support:
        dz_opt[z0 == 0] = 0

    dZs = 2 * tol * np.ones(n_seg)
    active_segs = np.array([True] * n_seg)
    i_seg, t_start_seg = 0, 0
    t_end_seg = n_times_seg
    for ii in range(max_iter):
        # Pick a coordinate to update
        if strategy == 'random':
            raise NotImplementedError()
        elif strategy == 'greedy':
            # if dZs[i_seg] > tol:
            if active_segs[i_seg]:
                i0 = np.argmax(np.abs(dz_opt[:, t_start_seg:t_end_seg]))
                n_times_current = min(n_times_seg, n_times_valid - t_start_seg)
                k0, t0 = np.unravel_index(i0, (n_atoms, n_times_current))
                t0 += t_start_seg
                dz = dz_opt[k0, t0]
                dZs[i_seg] = abs(dz)
            else:
                dz = tol
        else:
            raise ValueError('The coordinate selection method should be in '
                             "{'greedy' | 'random'}. Got {}.".format(strategy))

        # Update the selected coordinate and beta if the update is greater than
        # the convergence tolerance.
        if abs(dz) > tol:
            z_hat[k0, t0] += dz

            beta_i0 = beta[k0, t0]
            offset = max(0, n_times_atom - t0 - 1)
            t_start = max(0, t0 - n_times_atom + 1)
            t_end = min(t0 + n_times_atom, n_times_valid)
            ll = t_end - t_start
            beta[:, t_start:t_end] += DtD[:, k0, offset:offset + ll] * dz
            beta[k0, t0] = beta_i0
            dz_opt[:, t_start:t_end] = (
                np.maximum(-beta[:, t_start:t_end] - reg, 0) / norm_Dk
                - z_hat[:, t_start:t_end])
            dz_opt[k0, t0] = 0
            if t_start < t_start_seg and dZs[i_seg - 1] <= tol:
                dZs[i_seg - 1] = 2 * tol
                active_segs[i_seg - 1] = True
            if t_end > t_end_seg and dZs[i_seg + 1] <= tol:
                dZs[i_seg + 1] = 2 * tol
                active_segs[i_seg + 1] = True
            if freeze_support:
                dz_opt[:, t_start:t_end][z0[:, t_start:t_end] == 0] = 0
                nnz_z0 = list(zip(*z0[:, t_start:t_end].nonzero()))
                nnz_dz = list(zip(*dz_opt[:, t_start:t_end].nonzero()))
                assert all([nnz in nnz_z0 for nnz in nnz_dz])
        else:
            active_segs[i_seg] = False

        if debug:
            pobj.append(objective(z_hat))

        if dZs.max() <= tol:
            break

        i_seg += 1
        t_start_seg += n_times_seg
        t_end_seg += n_times_seg
        if t_start_seg >= n_times_valid:
            dZs[i_seg:] = 0  # Make sure that we do not miss some segments
            i_seg = 0
            t_start_seg = 0
            t_end_seg = n_times_seg

    else:
        if verbose > 10:
            print('[CD] update z did not converge')
    if verbose > 10:
        print('[CD] update z computed %d iterations' % (ii + 1))

    if debug:
        return z_hat, pobj
    return z_hat
