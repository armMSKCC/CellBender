import numpy as np
from typing import Tuple, List, Dict, Union

import pyro
from pyro import poutine
import pyro.distributions as dist

import torch
from torch.distributions import constraints
from torch.nn.parameter import Parameter

from pyro_extras import CustomLogProbTerm, NegativeBinomial, ZeroInflatedNegativeBinomial, \
    MixtureDistribution, logit, logaddexp, get_log_prob_compl
from sc_fingerprint import SingleCellFingerprintDataStore
from sc_fsd_codec import FamilySizeDistributionCodec, SortByComponentWeights

import logging
from collections import defaultdict


class SingleCellFamilySizeModel(torch.nn.Module):
    DEFAULT_E_LO_SUM_WIDTH = 10
    DEFAULT_E_HI_SUM_WIDTH = 20
    DEFAULT_CONFIDENCE_INTERVAL_LOWER = 0.05 
    DEFAULT_CONFIDENCE_INTERVAL_UPPER = 0.95
    EPS = 1e-6
    
    def __init__(self,
                 init_params_dict: dict,
                 model_constraint_params_dict: dict,
                 sc_fingerprint_datastore: SingleCellFingerprintDataStore,
                 fsd_codec: FamilySizeDistributionCodec,
                 guide_type: str = 'map',
                 device=torch.device('cuda'),
                 dtype=torch.float):
        super(SingleCellFamilySizeModel, self).__init__()

        self.model_constraint_params_dict = model_constraint_params_dict
        self.sc_fingerprint_datastore = sc_fingerprint_datastore
        self.fsd_codec = fsd_codec
        
        self.n_total_cells = sc_fingerprint_datastore.n_cells
        self.n_total_genes = sc_fingerprint_datastore.n_genes

        self.guide_type = guide_type

        self.device = device
        self.dtype = dtype

        # hyperparameters
        self.fsd_gmm_num_components = init_params_dict['fsd.gmm_num_components']
        self.fsd_gmm_dirichlet_concentration = init_params_dict['fsd.gmm_dirichlet_concentration']
        self.fsd_gmm_init_xi_scale = init_params_dict['fsd.gmm_init_xi_scale']
        self.fsd_gmm_min_xi_scale = init_params_dict['fsd.gmm_min_xi_scale']
        self.fsd_gmm_init_components_perplexity = init_params_dict['fsd.gmm_init_components_perplexity']
        self.fsd_gmm_min_weight_per_component = init_params_dict['fsd.gmm_min_weight_per_component']
        self.enable_fsd_w_dirichlet_reg = init_params_dict['fsd.enable_fsd_w_dirichlet_reg']
        self.w_lo_dirichlet_reg_strength = init_params_dict['fsd.w_lo_dirichlet_reg_strength']
        self.w_hi_dirichlet_reg_strength = init_params_dict['fsd.w_hi_dirichlet_reg_strength']
        self.w_lo_dirichlet_concentration = init_params_dict['fsd.w_lo_dirichlet_concentration']
        self.w_hi_dirichlet_concentration = init_params_dict['fsd.w_hi_dirichlet_concentration']
        self.train_chimera_rate_params = init_params_dict['chimera.enable_hyperparameter_optimization']
        self.fsd_xi_posterior_min_scale = init_params_dict['fsd.xi_posterior_min_scale']
        
        # empirical normalization factors
        self.median_total_reads_per_cell = np.median(sc_fingerprint_datastore.total_obs_reads_per_cell)
        self.median_fsd_mu_hi = np.median(sc_fingerprint_datastore.empirical_fsd_mu_hi)

        # initial parameters for e_lo
        self.init_alpha_c = init_params_dict['chimera.alpha_c']
        self.init_beta_c = init_params_dict['chimera.beta_c']

        # initial parameters for e_hi
        self.init_mu_e_hi = sc_fingerprint_datastore.empirical_mu_e_hi
        self.init_phi_e_hi = sc_fingerprint_datastore.empirical_phi_e_hi
        self.init_logit_p_zero_e_hi = logit(torch.tensor(sc_fingerprint_datastore.empirical_p_zero_e_hi)).numpy()

        # logging
        self._logger = logging.getLogger()
                
    def model(self, data):
        # register the parameters of the family size distribution codec
        pyro.module("fsd_codec", self.fsd_codec)
        
        # GMM prior for family size distribution parameters
        fsd_xi_prior_locs = pyro.param(
            "fsd_xi_prior_locs",
            self.fsd_codec.init_fsd_xi_loc_prior +
            self.fsd_gmm_init_components_perplexity * torch.randn(
                (self.fsd_gmm_num_components, self.fsd_codec.total_fsd_params),
                dtype=self.dtype, device=self.device))

        fsd_xi_prior_scales = pyro.param(
            "fsd_xi_prior_scales",
            self.fsd_gmm_init_xi_scale * torch.ones(
                (self.fsd_gmm_num_components, self.fsd_codec.total_fsd_params),
                dtype=self.dtype, device=self.device),
            constraint=constraints.greater_than(self.fsd_gmm_min_xi_scale))
        
        # chimera parameters
        alpha_c = pyro.param(
            "alpha_c",
            torch.tensor(self.init_alpha_c, device=self.device, dtype=self.dtype),
            constraint=constraints.positive)
        beta_c = pyro.param(
            "beta_c",
            torch.tensor(self.init_beta_c, device=self.device, dtype=self.dtype),
            constraint=constraints.positive)

        # gene expression parameters
        mu_e_hi = pyro.param(
            "mu_e_hi",
            torch.tensor(self.init_mu_e_hi, device=self.device, dtype=self.dtype),
            constraint=constraints.positive)
        phi_e_hi = pyro.param(
            "phi_e_hi",
            torch.tensor(self.init_phi_e_hi, device=self.device, dtype=self.dtype),
            constraint=constraints.positive)
        logit_p_zero_e_hi = pyro.param(
            "logit_p_zero_e_hi",
            torch.tensor(self.init_logit_p_zero_e_hi, device=self.device, dtype=self.dtype))

        # sizes
        mb_size = data['fingerprint_tensor'].shape[0]
        batch_shape = torch.Size([mb_size])
        max_family_size = data['fingerprint_tensor'].shape[1]
        
        # useful auxiliary quantities
        family_size_vector_observable = torch.arange(1, max_family_size + 1, device=self.device).type(self.dtype)
        family_size_vector_full = torch.arange(0, max_family_size + 1, device=self.device).type(self.dtype)
        zero = torch.tensor(0, device=self.device, dtype=self.dtype)

        if not self.train_chimera_rate_params:
            alpha_c = alpha_c.clone().detach()
            beta_c = beta_c.clone().detach()

        #  fsd xi prior distribution
        fsd_xi_prior_dist = self.get_fsd_xi_prior_dist(fsd_xi_prior_locs, fsd_xi_prior_scales)
        
        with pyro.plate("collapsed_gene_cell", size=mb_size):

            with poutine.scale(scale=data['gene_sampling_site_scale_factor_tensor']):
                # sample gene family size distribution parameters
                fsd_xi = pyro.sample("fsd_xi", fsd_xi_prior_dist)

            # transform to the constrained space
            fsd_params_dict = self.fsd_codec.decode(fsd_xi)

            # get chimeric and real family size distributions
            fsd_lo_dist, fsd_hi_dist = self.fsd_codec.get_fsd_components(
                fsd_params_dict,
                downsampling_rate_tensor=data['downsampling_rate_tensor'])

            # extract required quantities from the distributions
            mu_lo = fsd_lo_dist.mean.squeeze(-1)
            mu_hi = fsd_hi_dist.mean.squeeze(-1)
            log_p_unobs_lo = fsd_lo_dist.log_prob(zero).squeeze(-1)
            log_p_unobs_hi = fsd_hi_dist.log_prob(zero).squeeze(-1)
            log_p_obs_lo = get_log_prob_compl(log_p_unobs_lo)
            log_p_obs_hi = get_log_prob_compl(log_p_unobs_hi)
            p_obs_lo = log_p_obs_lo.exp()
            p_obs_hi = log_p_obs_hi.exp()

            # localization and/or calculation of required variables for pickup by locals() -- see below
            p_obs_lo_to_p_obs_hi_ratio = p_obs_lo / p_obs_hi
            phi_lo_comps = fsd_params_dict['phi_lo']
            phi_hi_comps = fsd_params_dict['phi_hi']
            mu_lo_comps = fsd_params_dict['mu_lo']
            mu_hi_comps = fsd_params_dict['mu_hi']
            w_lo_comps = fsd_params_dict['w_lo']
            w_hi_comps = fsd_params_dict['w_hi']
            mu_hi_comps_to_mu_empirical_ratio = mu_hi_comps / (
                self.EPS + data['empirical_fsd_mu_hi_tensor'].unsqueeze(-1))
            mu_lo_comps_to_mu_empirical_ratio = mu_lo_comps / (
                self.EPS + data['empirical_fsd_mu_hi_tensor'].unsqueeze(-1))
            
            # observation probability for each component of the distribution
            alpha_lo_comps = (self.EPS + phi_lo_comps).reciprocal()
            log_p_unobs_lo_comps = alpha_lo_comps * (alpha_lo_comps.log() - (alpha_lo_comps + mu_lo_comps).log())
            p_obs_lo_comps = get_log_prob_compl(log_p_unobs_lo_comps).exp()
            alpha_hi_comps = (self.EPS + phi_hi_comps).reciprocal()
            log_p_unobs_hi_comps = alpha_hi_comps * (alpha_hi_comps.log() - (alpha_hi_comps + mu_hi_comps).log())
            p_obs_hi_comps = get_log_prob_compl(log_p_unobs_hi_comps).exp()
            
            # slicing expression mu and phi by gene_index_tensor -- we only need these slices later on
            phi_e_hi_batch = phi_e_hi[data['gene_index_tensor']]
            mu_e_hi_batch = mu_e_hi[data['gene_index_tensor']]
            logit_p_zero_e_hi_batch = logit_p_zero_e_hi[data['gene_index_tensor']]

            # add FSD sparsity regularization to log likelihood
            if self.enable_fsd_w_dirichlet_reg:
                self.sample_fsd_weight_sparsity_regularization(
                    fsd_params_dict,
                    data['gene_sampling_site_scale_factor_tensor'])

            # add (soft) constraints to log likelihood
            model_vars_dict = locals()
            self.sample_gene_plate_soft_constraints(
                model_vars_dict,
                data['gene_sampling_site_scale_factor_tensor'],
                batch_shape)

            # empirical "cell size" scale estimate
            cell_size_scale = data['total_obs_reads_per_cell_tensor'] / (
                self.median_total_reads_per_cell * data['downsampling_rate_tensor'])

            # calculate the (poisson) rate of chimeric molecule formation
            e_hi_prior_dist_global = ZeroInflatedNegativeBinomial(
                logit_zero=logit_p_zero_e_hi_batch,
                mu=mu_e_hi_batch,
                phi=phi_e_hi_batch)
            mean_e_hi = e_hi_prior_dist_global.mean
            normalized_total_fragments = mean_e_hi * mu_hi / (
                self.median_fsd_mu_hi * data['downsampling_rate_tensor'])
            mu_e_lo = (alpha_c + beta_c * cell_size_scale) * normalized_total_fragments

            # calculate p_lo and p_hi on all observable family sizes
            log_prob_p_lo_full = fsd_lo_dist.log_prob(family_size_vector_full)
            log_prob_p_hi_full = fsd_hi_dist.log_prob(family_size_vector_full)
            log_prob_p_lo_obs = log_prob_p_lo_full[..., 1:]
            log_prob_p_hi_obs = log_prob_p_hi_full[..., 1:]

            fingerprint_log_likelihood = self.get_fingerprint_log_likelihood_monte_carlo(
                data['fingerprint_tensor'],
                log_prob_p_lo_obs,
                log_prob_p_hi_obs
            )

            # observe
            with poutine.scale(scale=data['cell_sampling_site_scale_factor_tensor']):
                pyro.sample("fingerprint_and_expression_observation",
                            CustomLogProbTerm(
                                custom_log_prob=fingerprint_log_likelihood,
                                batch_shape=batch_shape,
                                event_shape=torch.Size([])),
                            obs=torch.zeros_like(fingerprint_log_likelihood))

    @staticmethod
    def get_fingerprint_log_likelihood_monte_carlo(fingerprint_tensor: torch.Tensor,
                                                   log_prob_p_lo_obs: torch.Tensor,
                                                   log_prob_p_hi_obs: torch.Tensor,
                                                   mu_e_lo: torch.Tensor,
                                                   mu_e_hi: torch.Tensor,
                                                   phi_e_hi: torch.Tensor,
                                                   logit_p_zero_e_hi: torch.Tensor,

                                                   ) -> torch.Tensor:


    def get_fsd_xi_prior_dist(self, fsd_xi_prior_locs, fsd_xi_prior_scales):
        if self.fsd_gmm_num_components > 1:
            # generate the marginalized GMM distribution
            fsd_xi_prior_weights = pyro.sample(
                "fsd_xi_prior_weights",
                dist.Dirichlet(
                    self.fsd_gmm_dirichlet_concentration *
                    torch.ones((self.fsd_gmm_num_components,), dtype=self.dtype, device=self.device)))
            fsd_xi_prior_log_weights = fsd_xi_prior_weights.log()
            fsd_xi_prior_log_weights_tuple = tuple(
                fsd_xi_prior_log_weights[j]
                for j in range(self.fsd_gmm_num_components))
            fsd_xi_prior_components_tuple = tuple(
                dist.Normal(fsd_xi_prior_locs[j, :], fsd_xi_prior_scales[j, :]).to_event(1)
                for j in range(self.fsd_gmm_num_components))
            fsd_xi_prior_dist = MixtureDistribution(
                fsd_xi_prior_log_weights_tuple, fsd_xi_prior_components_tuple)
        else:
            fsd_xi_prior_dist = dist.Normal(fsd_xi_prior_locs[0, :], fsd_xi_prior_scales[0, :]).to_event(1)
        return fsd_xi_prior_dist

    def sample_gene_plate_soft_constraints(self, model_vars_dict, scale_factor_tensor, batch_shape):
        with poutine.scale(scale=scale_factor_tensor):
            for var_name, var_constraint_params in self.model_constraint_params_dict.items():
                var = model_vars_dict[var_name]
                if 'lower_bound_value' in var_constraint_params:
                    value = var_constraint_params['lower_bound_value']
                    width = var_constraint_params['lower_bound_width']
                    exponent = var_constraint_params['lower_bound_exponent']
                    strength = var_constraint_params['lower_bound_strength']
                    if isinstance(value, str):
                        value = model_vars_dict[value]
                    activity = torch.clamp(value + width - var, min=0.) / width
                    constraint_log_prob = - strength * activity.pow(exponent)
                    for _ in range(len(var.shape) - 1):
                        constraint_log_prob = constraint_log_prob.sum(-1)
                    pyro.sample(
                        var_name + "_lower_bound_constraint",
                        CustomLogProbTerm(constraint_log_prob,
                                          batch_shape=batch_shape,
                                          event_shape=torch.Size([])),
                        obs=torch.zeros_like(constraint_log_prob))

                if 'upper_bound_value' in var_constraint_params:
                    value = var_constraint_params['upper_bound_value']
                    width = var_constraint_params['upper_bound_width']
                    exponent = var_constraint_params['upper_bound_exponent']
                    strength = var_constraint_params['upper_bound_strength']
                    if isinstance(value, str):
                        value = model_vars_dict[value]
                    activity = torch.clamp(var - value + width, min=0.) / width
                    constraint_log_prob = - strength * activity.pow(exponent)
                    for _ in range(len(var.shape) - 1):
                        constraint_log_prob = constraint_log_prob.sum(-1)
                    pyro.sample(
                        var_name + "_upper_bound_constraint",
                        CustomLogProbTerm(constraint_log_prob,
                                          batch_shape=batch_shape,
                                          event_shape=torch.Size([])),
                        obs=torch.zeros_like(constraint_log_prob))

                if 'pin_value' in var_constraint_params:
                    value = var_constraint_params['pin_value']
                    exponent = var_constraint_params['pin_exponent']
                    strength = var_constraint_params['pin_strength']
                    if isinstance(value, str):
                        value = model_vars_dict[value]
                    activity = (var - value).abs()
                    constraint_log_prob = - strength * activity.pow(exponent)
                    for _ in range(len(var.shape) - 1):
                        constraint_log_prob = constraint_log_prob.sum(-1)
                    pyro.sample(
                        var_name + "_pin_value_constraint",
                        CustomLogProbTerm(constraint_log_prob,
                                          batch_shape=batch_shape,
                                          event_shape=torch.Size([])),
                        obs=torch.zeros_like(constraint_log_prob))

    def sample_fsd_weight_sparsity_regularization(self, fsd_params_dict, scale_factor_tensor):
        with poutine.scale(scale=scale_factor_tensor):
            if self.fsd_codec.n_fsd_lo_comps > 1:
                with poutine.scale(scale=self.w_lo_dirichlet_reg_strength):
                    pyro.sample(
                        "w_lo_dirichlet_reg",
                        dist.Dirichlet(
                            self.w_lo_dirichlet_concentration * torch.ones_like(fsd_params_dict['w_lo'])),
                        obs=fsd_params_dict['w_lo'])
            if self.fsd_codec.n_fsd_hi_comps > 1:
                with poutine.scale(scale=self.w_hi_dirichlet_reg_strength):
                    pyro.sample(
                        "w_hi_dirichlet_reg",
                        dist.Dirichlet(
                            self.w_hi_dirichlet_concentration * torch.ones_like(fsd_params_dict['w_hi'])),
                        obs=fsd_params_dict['w_hi'])

    def guide(self, data):
        if self.fsd_gmm_num_components > 1:
            # MAP estimate of GMM fsd prior weights
            fsd_xi_prior_weights_map = pyro.param(
                "fsd_xi_prior_weights_map",
                torch.ones((self.fsd_gmm_num_components,),
                           device=self.device, dtype=self.dtype) / self.fsd_gmm_num_components,
                constraint=constraints.simplex)
            fsd_xi_prior_weights = pyro.sample("fsd_xi_prior_weights", dist.Delta(
                self.fsd_gmm_min_weight_per_component +
                (1 - self.fsd_gmm_num_components * self.fsd_gmm_min_weight_per_component) * fsd_xi_prior_weights_map))

        # point estimate for fsd_xi (gene)
        fsd_xi_posterior_loc = pyro.param(
            "fsd_xi_posterior_loc",
            self.fsd_codec.get_sorted_fsd_xi(self.fsd_codec.init_fsd_xi_loc_posterior))
        
        # base posterior distribution for xi
        if self.guide_type == 'map':
            fsd_xi_posterior_base_dist = dist.Delta(
                v=fsd_xi_posterior_loc[data['gene_index_tensor'], :]).to_event(1)
        elif self.guide_type == 'gaussian':
            fsd_xi_posterior_scale = pyro.param(
                "fsd_xi_posterior_scale",
                self.fsd_gmm_init_xi_scale * torch.ones(
                    (self.n_total_genes, self.fsd_codec.total_fsd_params), device=self.device, dtype=self.dtype),
                constraint=constraints.greater_than(self.fsd_xi_posterior_min_scale))
            fsd_xi_posterior_base_dist = dist.Normal(
                loc=fsd_xi_posterior_loc[data['gene_index_tensor'], :],
                scale=fsd_xi_posterior_scale[data['gene_index_tensor'], :]).to_event(1)
        else:
            raise Exception("Unknown guide_type!")
        
        # apply a pseudo-bijective transformation to sort xi by component weights
        fsd_xi_sort_trans = SortByComponentWeights(self.fsd_codec)
        fsd_xi_posterior_dist = dist.TransformedDistribution(
            fsd_xi_posterior_base_dist, [fsd_xi_sort_trans])
        
        mb_size = data['fingerprint_tensor'].shape[0]
        with pyro.plate("collapsed_gene_cell", size=mb_size):
            with poutine.scale(scale=data['gene_sampling_site_scale_factor_tensor']):
                pyro.sample("fsd_xi", fsd_xi_posterior_dist)

    def get_active_constraints_on_genes(self) -> Dict:
        empirical_fsd_mu_hi_tensor = torch.tensor(
            self.sc_fingerprint_datastore.empirical_fsd_mu_hi, device=self.device, dtype=self.dtype)
        zero = torch.tensor(0, device=self.device, dtype=self.dtype)
        active_constraints_dict = defaultdict(dict)

        with torch.no_grad():
            fsd_xi = pyro.param("fsd_xi_posterior_loc")

            # transform to the constrained space
            fsd_params_dict = self.fsd_codec.decode(fsd_xi)

            # get chimeric and real family size distributions
            fsd_lo_dist, fsd_hi_dist = self.fsd_codec.get_fsd_components(fsd_params_dict, None)

            # extract required quantities from the distributions
            mu_lo = fsd_lo_dist.mean.squeeze(-1)
            mu_hi = fsd_hi_dist.mean.squeeze(-1)
            log_p_unobs_lo = fsd_lo_dist.log_prob(zero).squeeze(-1)
            log_p_unobs_hi = fsd_hi_dist.log_prob(zero).squeeze(-1)
            log_p_obs_lo = get_log_prob_compl(log_p_unobs_lo)
            log_p_obs_hi = get_log_prob_compl(log_p_unobs_hi)
            p_obs_lo = log_p_obs_lo.exp()
            p_obs_hi = log_p_obs_hi.exp()

            # localization and/or calculation of required variables for pickup by locals()
            p_obs_lo_to_p_obs_hi_ratio = p_obs_lo / p_obs_hi
            phi_lo_comps = fsd_params_dict['phi_lo']
            phi_hi_comps = fsd_params_dict['phi_hi']
            mu_lo_comps = fsd_params_dict['mu_lo']
            mu_hi_comps = fsd_params_dict['mu_hi']
            w_lo_comps = fsd_params_dict['w_lo']
            w_hi_comps = fsd_params_dict['w_hi']
            mu_hi_comps_to_mu_empirical_ratio = mu_hi_comps / (
                self.EPS + empirical_fsd_mu_hi_tensor.unsqueeze(-1))
            mu_lo_comps_to_mu_empirical_ratio = mu_lo_comps / (
                self.EPS + empirical_fsd_mu_hi_tensor.unsqueeze(-1))
            alpha_lo_comps = (self.EPS + phi_lo_comps).reciprocal()
            log_p_unobs_lo_comps = alpha_lo_comps * (alpha_lo_comps.log() - (alpha_lo_comps + mu_lo_comps).log())
            p_obs_lo_comps = get_log_prob_compl(log_p_unobs_lo_comps).exp()
            alpha_hi_comps = (self.EPS + phi_hi_comps).reciprocal()
            log_p_unobs_hi_comps = alpha_hi_comps * (alpha_hi_comps.log() - (alpha_hi_comps + mu_hi_comps).log())
            p_obs_hi_comps = get_log_prob_compl(log_p_unobs_hi_comps).exp()
            phi_e_lo_batch = pyro.param("phi_e_lo")
            phi_e_hi_batch = pyro.param("phi_e_hi")
            mu_e_hi_batch = pyro.param("mu_e_hi")
            logit_p_zero_e_hi_batch = pyro.param("logit_p_zero_e_hi")

            model_vars_dict = locals()
            for var_name, var_constraint_params in self.model_constraint_params_dict.items():
                var = model_vars_dict[var_name]
                if 'lower_bound_value' in var_constraint_params:
                    value = var_constraint_params['lower_bound_value']
                    width = var_constraint_params['lower_bound_width']
                    if isinstance(value, str):
                        value = model_vars_dict[value]
                    activity = torch.clamp(value + width - var, min=0.)
                    for _ in range(len(var.shape) - 1):
                        activity = activity.sum(-1)
                    nnz_activity = torch.nonzero(activity).cpu().numpy().flatten()
                    if nnz_activity.size > 0:
                        active_constraints_dict[var_name]['lower_bound'] = set(nnz_activity.tolist())

                if 'upper_bound_value' in var_constraint_params:
                    value = var_constraint_params['upper_bound_value']
                    width = var_constraint_params['upper_bound_width']
                    exponent = var_constraint_params['upper_bound_exponent']
                    strength = var_constraint_params['upper_bound_strength']
                    if isinstance(value, str):
                        value = model_vars_dict[value]
                    activity = torch.clamp(var - value + width, min=0.)
                    for _ in range(len(var.shape) - 1):
                        activity = activity.sum(-1)
                    nnz_activity = torch.nonzero(activity).cpu().numpy().flatten()
                    if nnz_activity.size > 0:
                        active_constraints_dict[var_name]['upper_bound'] = set(nnz_activity.tolist())

        return dict(active_constraints_dict)
