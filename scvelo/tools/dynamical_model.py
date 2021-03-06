from .. import settings
from .. import logging as logg
from .utils import make_dense, make_unique_list
from .dynamical_model_utils import BaseDynamics, unspliced, spliced, vectorize, derivatives, \
    find_swichting_time, fit_alpha, fit_scaling, linreg, convolve, assign_timepoints

import numpy as np
import matplotlib.pyplot as pl
from matplotlib import rcParams


class DynamicsRecovery(BaseDynamics):
    def __init__(self, adata=None, gene=None, u=None, s=None, use_raw=False, load_pars=None):
        super(DynamicsRecovery, self).__init__(adata.n_obs)

        _layers = adata[:, gene].layers
        self.use_raw = use_raw = use_raw or 'Ms' not in _layers.keys()

        # extract actual data
        if u is None or s is None:
            u = make_dense(_layers['unspliced']) if use_raw else make_dense(_layers['Mu'])
            s = make_dense(_layers['spliced']) if use_raw else make_dense(_layers['Ms'])
        self.s, self.u = s, u

        s_raw = s if use_raw else make_dense(_layers['spliced'])
        u_raw = u if use_raw else make_dense(_layers['unspliced'])

        # set weights for fitting (exclude dropouts and extreme outliers)
        s_filter = np.ravel(s_raw > 0)
        u_filter = np.ravel(u_raw > 0)
        s_filter &= np.ravel(s < np.percentile(s[s_filter], 99))
        u_filter &= np.ravel(u < np.percentile(u[u_filter], 99))

        self.weights = s_filter & u_filter

        if load_pars and 'fit_alpha' in adata.var.keys():
            self.load_pars(adata, gene)
        else:
            self.initialize()

    def initialize(self):
        self.scaling = self.u.sum(0) / self.s.sum(0) * 1.3
        u, s, w = self.u / self.scaling, self.s, self.weights
        u_w, s_w, perc = u[w], s[w], 95
        # initialize beta and gamma from extreme quantiles of s
        weights_s = s_w >= np.percentile(s_w, perc, axis=0)
        beta, gamma = 1, linreg(convolve(u_w, weights_s), convolve(s_w, weights_s))

        # initialize alpha and switching points from extreme quantiles of u
        weights_u = u_w >= np.percentile(u_w, perc, axis=0)
        u_w, s_w = u_w[weights_u], s_w[weights_u]

        alpha, u0_, s0_ = u_w.mean(), u_w.mean(), s_w.mean()
        alpha_, u0, s0, = 0, 0, 0

        t, tau, o = assign_timepoints(u, s, alpha, beta, gamma, u0_=u0_, s0_=s0_)

        # update object with initialized vars
        self.alpha, self.beta, self.gamma, self.alpha_ = alpha, beta, gamma, alpha_
        self.u0, self.s0, self.u0_, self.s0_ = u0, s0, u0_, s0_
        self.t, self.tau, self.o, self.t_ = t, tau, o, np.max(tau * o)
        self.pars = np.array([alpha, beta, gamma, self.t_, self.scaling])[:, None]

        self.loss = [self.get_loss()]
        self.update_state_dependent()
        self.update_scaling()

    def load_pars(self, adata, gene):
        idx = np.where(adata.var_names == gene)[0][0] if isinstance(gene, str) else gene
        self.alpha = adata.var['fit_alpha'][idx]
        self.beta = adata.var['fit_beta'][idx]
        self.gamma = adata.var['fit_gamma'][idx]
        self.scaling = adata.var['fit_scaling'][idx]
        self.t_ = adata.var['fit_t_'][idx]
        self.t = adata.layers['fit_t'][:, idx]

        self.u0, self.s0, self.alpha_ = 0, 0, 0
        self.u0_ = unspliced(self.t_, self.u0, self.alpha, self.beta)
        self.s0_ = spliced(self.t_, self.u0, self.s0, self.alpha, self.beta, self.gamma)
        self.update_state_dependent()

    def fit(self, max_iter=100, r=None, method=None, clip_loss=None):
        improved, idx_update = True, np.clip(int(max_iter / 10), 1, None)

        for i in range(max_iter):
            self.update_vars(r=r, method=method, clip_loss=clip_loss)
            if improved or (i % idx_update == 1) or i == max_iter - 1:
                improved = self.update_state_dependent()
            if i > 5:
                loss_prev, loss = np.max(self.loss[-5:]), self.loss[-1]
                if loss_prev - loss < loss_prev * .001:
                    improved = self.shuffle_pars()
                    if not improved:
                        break

    def update_state_dependent(self):
        u, s, w = self.u / self.scaling, self.s, self.weights
        u_w, s_w = (u, s) if w is None else (u[w], s[w])
        alpha, beta, gamma, scaling = self.alpha, self.beta, self.gamma, self.scaling

        improved_tau, improved_alpha, improved_scaling = False, False, False
        # find optimal switching (generalized lin.reg) & assign timepoints/states (explicit)
        tau_w, o_w = (self.tau, self.o) if w is None else (self.tau[w], self.o[w])
        t0_ = find_swichting_time(u_w, s_w, tau_w, o_w, alpha, beta, gamma)

        t0_vals = t0_ + np.linspace(-1, 1, num=5) * t0_ / 10
        for t0_ in t0_vals:
            improved_tau = improved_tau or self.update_loss(t_=t0_, reassign_time=True)  # update if improved

        # fit alpha (generalized lin.reg)
        tau_w, o_w = (self.tau, self.o) if w is None else (self.tau[w], self.o[w])
        alpha = fit_alpha(u_w, s_w, tau_w, o_w, beta, gamma)

        alpha_vals = alpha + np.linspace(-1, 1, num=5) * alpha / 10
        for alpha in alpha_vals:
            improved_alpha = improved_alpha or self.update_loss(alpha=alpha, reassign_time=True)  # update if improved

        # fit scaling (generalized lin.reg)
        t_w, tau_w, o_w = (self.t, self.tau, self.o) if w is None else (self.t[w], self.tau[w], self.o[w])
        alpha, t0_ = self.alpha, self.t_
        scaling = fit_scaling(u_w, t_w, t0_, alpha, beta)
        improved_scaling = self.update_loss(scaling=scaling * self.scaling, reassign_time=True)  # update if improved

        return improved_tau or improved_alpha or improved_scaling

    def update_scaling(self):
        # fit scaling and update if improved
        u, s, t, tau, o = self.u / self.scaling, self.s, self.t, self.tau, self.o
        alpha, beta, gamma, alpha_ = self.alpha, self.beta, self.gamma, self.alpha_
        u0, s0, u0_, s0_, t_ = self.u0, self.s0, self.u0_, self.s0_, 0 if self.t_ is None else self.t_

        # fit alpha and scaling and update if improved
        alpha = fit_alpha(u, s, tau, o, beta, gamma)
        t0_ = find_swichting_time(u, s, tau, o, alpha, beta, gamma)
        t, tau, o = assign_timepoints(u, s, alpha, beta, gamma, t0_)
        improved_alpha = self.update_loss(t, t0_, alpha=alpha)

        # fit scaling and update if improved
        scaling = fit_scaling(u, t, t_, alpha, beta) * self.scaling
        t0_ = find_swichting_time(u, s, tau, o, alpha, beta, gamma)
        t, tau, o = assign_timepoints(u, s, alpha, beta, gamma, t0_)
        improved_scaling = self.update_loss(t, t0_, scaling=scaling)

        return improved_alpha or improved_scaling

    def update_vars(self, r=None, method=None, clip_loss=None):
        if r is None:
            r = 1e-2 if method is 'adam' else 1e-6
        if clip_loss is None:
            clip_loss = False if method is 'adam' else True
        # if self.weights is None:
        #    self.uniform_weighting(n_regions=5, perc=95)
        t, t_, alpha, beta, gamma, scaling = self.t, self.t_, self.alpha, self.beta, self.gamma, self.scaling
        dalpha, dbeta, dgamma, dalpha_, dtau, dt_ = derivatives(self.u, self.s, t, t_, alpha, beta, gamma, scaling)

        if method is 'adam':
            b1, b2, eps = 0.9, 0.999, 1e-8

            # update 1st and 2nd order gradient moments
            dpars = np.array([dalpha, dbeta, dgamma])
            m_dpars = b1 * self.m_dpars[:, -1] + (1 - b1) * dpars
            v_dpars = b2 * self.v_dpars[:, -1] + (1 - b2) * dpars**2

            self.dpars = np.c_[self.dpars, dpars]
            self.m_dpars = np.c_[self.m_dpars, m_dpars]
            self.v_dpars = np.c_[self.v_dpars, v_dpars]

            # correct for bias
            t = len(self.m_dpars[0])
            m_dpars /= (1 - b1 ** t)
            v_dpars /= (1 - b2 ** t)

            # Adam parameter update
            alpha -= r * m_dpars[0] / (np.sqrt(v_dpars[0]) + eps)
            beta -= r * m_dpars[1] / (np.sqrt(v_dpars[1]) + eps)
            gamma -= r * m_dpars[2] / (np.sqrt(v_dpars[2]) + eps)

        else:
            alpha -= r * dalpha
            beta -= r * dbeta
            gamma -= r * dgamma
            # tau -= r * dtau
            # t_ -= r * dt_
            # t_ = np.max(self.tau * self.o)
            # t = tau * self.o + (tau + t_) * (1 - self.o)

        improved_vars = self.update_loss(alpha=alpha, beta=beta, gamma=gamma, clip_loss=clip_loss)

    def update_loss(self, t=None, t_=None, alpha=None, beta=None, gamma=None, scaling=None, reassign_time=False,
                    clip_loss=True, report=False):
        vals = [t_, alpha, beta, gamma, scaling]
        vals_prev = [self.t_, self.alpha, self.beta, self.gamma, self.scaling]
        vals_name = ['t_', 'alpha', 'beta', 'gamma', 'scaling']
        new_vals, new_vals_prev, new_vals_name = [], [], []
        loss_prev = self.loss[-1] if len(self.loss) > 0 else 1e6

        for val, val_prev, val_name in zip(vals, vals_prev, vals_name):
            if val is not None:
                new_vals.append(val)
                new_vals_prev.append(val_prev)
                new_vals_name.append(val_name)

        if reassign_time:
            t_ = self.get_optimal_switch(alpha, beta, gamma) if t_ is None else t_
            t, tau, o = self.get_time_assignment(t_, alpha, beta, gamma)

        loss = self.get_loss(t, t_, alpha, beta, gamma, scaling)
        perform_update = not clip_loss or loss < loss_prev

        if perform_update:
            if len(self.loss) > 0 and loss_prev - loss > loss_prev * .01 and report:  # improvement by at least 1%
                print('Update:',
                      ' '.join(map(str, new_vals_name)),
                      ' '.join(map(str, np.round(new_vals_prev, 2))), '-->',
                      ' '.join(map(str, np.round(new_vals, 2))))

                print('    loss:', np.round(loss_prev, 2), '-->', np.round(loss, 2))

            if 't_' in new_vals_name or reassign_time:
                self.t = t
                self.t_ = t_
                self.o = o = np.array(t <= t_, dtype=bool)
                self.tau = t * o + (t - t_) * (1 - o)

            if 'alpha' in new_vals_name: self.alpha = alpha
            if 'beta' in new_vals_name: self.beta = beta
            if 'gamma' in new_vals_name: self.gamma = gamma
            if 'scaling' in new_vals_name: self.scaling = scaling

        self.pars = np.c_[self.pars, np.array([self.alpha, self.beta, self.gamma, self.t_, self.scaling])[:, None]]
        self.loss.append(loss if perform_update else loss_prev)

        return perform_update

    def shuffle_pars(self, alpha_sight=[-.5, .5], gamma_sight=[-.5, .5], num=5):
        alpha_vals = np.linspace(alpha_sight[0], alpha_sight[1], num=num) * self.alpha + self.alpha
        gamma_vals = np.linspace(gamma_sight[0], gamma_sight[1], num=num) * self.gamma + self.gamma

        x, y = alpha_vals, gamma_vals
        f = lambda x, y: self.get_loss(alpha=x, gamma=y, reassign_time=True)
        z = np.zeros((len(x), len(x)))

        for i, xi in enumerate(x):
            for j, yi in enumerate(y):
                z[i, j] = f(xi, yi)
        ix, iy = np.unravel_index(z.argmin(), z.shape)
        return self.update_loss(alpha=x[ix], gamma=y[ix], reassign_time=True)


def read_pars(adata, pars_names=['alpha', 'beta', 'gamma', 't_', 'scaling'], key='fit'):
    pars = []
    for name in pars_names:
        pkey = key + '_' + name
        par = adata.var[pkey].values if pkey in adata.var.keys() else np.zeros(adata.n_vars) * np.nan
        pars.append(par)
    return pars


def write_pars(adata, pars, pars_names=['alpha', 'beta', 'gamma', 't_', 'scaling'], add_key='fit'):
    for i, name in enumerate(pars_names):
        adata.var[add_key + '_' + name] = pars[i]


def recover_dynamics(data, var_names='all', max_iter=100, learning_rate=None, add_key='fit', t_max=None, use_raw=False,
                     load_pars=None, return_model=False, plot_results=False, copy=False, **kwargs):
    """Estimates velocities in a gene-specific manner

    Arguments
    ---------
    data: :class:`~anndata.AnnData`
        Annotated data matrix.

    Returns
    -------
    Returns or updates `adata`
    """
    adata = data.copy() if copy else data
    logg.info('recovering dynamics', r=True)

    if var_names is 'all':
        idx = np.ones(adata.n_vars, dtype=bool)
    else:
        var_names = make_unique_list(var_names, allow_array=True)
        idx = adata.var_names.isin(var_names)
    if 'velocity_genes' in var_names and 'velocity_genes' in adata.var.keys():
        idx = idx & np.array(adata.var.velocity_genes.values)
    idx = np.where(idx)[0]
    var_names = adata.var_names[idx]

    alpha, beta, gamma, t_, scaling = read_pars(adata)
    L, P, T = [], [], adata.layers['fit_t'] if 'fit_t' in adata.layers.keys() else np.zeros(adata.shape) * np.nan

    for i, gene in enumerate(var_names):
        dm = DynamicsRecovery(adata, gene, use_raw=use_raw, load_pars=load_pars)
        if max_iter > 1:
            dm.fit(max_iter, learning_rate, **kwargs)

        ix = idx[i]
        alpha[ix], beta[ix], gamma[ix], t_[ix], scaling[ix] = dm.alpha, dm.beta, dm.gamma, dm.t_, dm.scaling
        T[:, ix] = dm.t
        L.append(dm.loss)
        if plot_results and i < 4:
            P.append(dm.pars)

    m = t_max / T.max(0) if t_max is not None else np.ones(adata.n_vars)
    alpha, beta, gamma, T, t_ = alpha / m, beta / m, gamma / m, T * m, t_ * m

    write_pars(adata, [alpha, beta, gamma, t_, scaling])
    adata.layers['fit_t'] = T

    cur_len = adata.varm['loss'].shape[1] if 'loss' in adata.varm.keys() else 2
    max_len = max(np.max([len(l) for l in L]), cur_len)
    loss = np.ones((adata.n_vars, max_len)) * np.nan

    if 'loss' in adata.varm.keys():
        loss[:, :cur_len] = adata.varm['loss']

    loss[idx] = np.vstack([np.concatenate([l, np.ones(max_len-len(l)) * np.nan]) for l in L])
    adata.varm['loss'] = loss

    logg.info('    finished', time=True, end=' ' if settings.verbosity > 2 else '\n')
    logg.hint('added \n' 
              '    \'' + add_key + '_pars' + '\', fitted parameters for splicing dynamics (adata.var)')

    if plot_results:  # Plot Parameter Stats
        figsize = [12, 5]  # rcParams['figure.figsize']
        fontsize = rcParams['font.size']
        fig, axes = pl.subplots(nrows=len(var_names[:4]), ncols=6, figsize=figsize)
        pl.subplots_adjust(wspace=0.7, hspace=0.5)
        for i, gene in enumerate(var_names[:4]):
            P[i] *= np.array([1 / m[idx[i]], 1 / m[idx[i]], 1 / m[idx[i]], m[idx[i]], 1])[:, None]
            for j, pij in enumerate(P[i]):
                axes[i][j].plot(pij)
            axes[i][len(P[i])].plot(L[i])
            if i == 0:
                for j, name in enumerate(['alpha', 'beta', 'gamma', 't_', 'scaling', 'loss']):
                    axes[i][j].set_title(name, fontsize=fontsize)

    return dm if return_model else adata if copy else None
