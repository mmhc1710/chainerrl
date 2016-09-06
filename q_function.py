from __future__ import division
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import absolute_import
from builtins import super
from builtins import range
from future import standard_library
standard_library.install_aliases()
import random

import numpy as np
import chainer
from chainer import functions as F
from chainer import links as L
from chainer import cuda

from links.mlp_bn import MLPBN
from links.mlp import MLP
from q_output import DiscreteQOutput
from q_output import ContinuousQOutput
from functions.lower_triangular_matrix import lower_triangular_matrix


class QFunction(object):
    pass


class StateInputQFunction(QFunction):
    """
    Input: state
    Output: values for each action
    """

    def forward(self, state, test=False):
        raise NotImplementedError()

    def __call__(self, state, action):
        assert state.shape[0] == 1
        xp = cuda.get_array_module(state)
        values = self.forward(state)
        q = F.select_item(
            values, chainer.Variable(xp.asarray(action, dtype=np.int32)))
        return q

    def sample_epsilon_greedily_with_value(self, state, epsilon):
        assert state.shape[0] == 1
        xp = cuda.get_array_module(state)
        values = self.forward(state)
        if random.random() < epsilon:
            a = random.randint(0, self.n_actions - 1)
        else:
            a = values.data[0].argmax()
        q = F.select_item(
            values, chainer.Variable(xp.asarray([a], dtype=np.int32)))
        return [a], q

    def sample_greedily_with_value(self, state):
        assert state.shape[0] == 1
        xp = cuda.get_array_module(state)
        values = self.forward(state)
        a = values.data[0].argmax()
        q = F.select_item(
            values, chainer.Variable(xp.asarray([a], dtype=np.int32)))
        return [a], q


class FCSIQFunction(chainer.ChainList, QFunction):
    """Fully-connected state-input (discrete) Q-function."""

    def __init__(self, n_input_channels, n_actions, n_hidden_channels,
                 n_hidden_layers):
        self.n_input_channels = n_input_channels
        self.n_actions = n_actions
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels

        layers = []
        assert n_hidden_layers >= 1
        layers.append(L.Linear(n_input_channels, n_hidden_channels))
        for i in range(n_hidden_layers - 1):
            layers.append(L.Linear(n_hidden_channels, n_hidden_channels))
        layers.append(L.Linear(n_hidden_channels, n_actions))

        super(FCSIQFunction, self).__init__(*layers)

    def __call__(self, state, test=False):
        h = state
        for layer in self[:-1]:
            h = F.elu(layer(h))
        h = self[-1](h)
        return DiscreteQOutput(h)


def scale_by_tanh(x, low, high):
    xp = cuda.get_array_module(x.data)
    scale = (high - low) / 2
    scale = xp.expand_dims(xp.asarray(scale, dtype=np.float32), axis=0)
    mean = (high + low) / 2
    mean = xp.expand_dims(xp.asarray(mean, dtype=np.float32), axis=0)
    return F.tanh(x) * scale + mean


class FCSIContinuousQFunction(chainer.Chain, QFunction):
    """Fully-connected state-input continuous Q-function."""

    def __init__(self, n_input_channels, n_dim_action, n_hidden_channels,
                 n_hidden_layers, action_space, scale_mu=True):
        """
        Args:
          n_input_channels: number of input channels
          n_dim_action: number of dimensions of action space
          n_hidden_channels: number of hidden channels
          n_hidden_layers: number of hidden layers
          action_space: action_space
          scale_mu (bool): scale mu by applying tanh if True
        """

        self.n_input_channels = n_input_channels
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels

        assert action_space is not None
        self.scale_mu = scale_mu
        self.action_space = action_space

        layers = {}

        hidden_layers = []
        assert n_hidden_layers >= 1
        hidden_layers.append(L.Linear(n_input_channels, n_hidden_channels))
        for i in range(n_hidden_layers - 1):
            hidden_layers.append(
                L.Linear(n_hidden_channels, n_hidden_channels))
        layers['hidden_layers'] = chainer.ChainList(*hidden_layers)

        layers['v'] = L.Linear(n_hidden_channels, 1)
        layers['mu'] = L.Linear(n_hidden_channels, n_dim_action)
        layers['mat_diag'] = L.Linear(n_hidden_channels, n_dim_action)
        non_diag_size = n_dim_action * (n_dim_action - 1) // 2
        if non_diag_size > 0:
            layers['mat_non_diag'] = L.Linear(n_hidden_channels, non_diag_size)

        super().__init__(**layers)

    def __call__(self, state, test=False):
        xp = cuda.get_array_module(state)
        h = state
        for layer in self.hidden_layers:
            h = F.elu(layer(h))
        v = self.v(h)
        mu = self.mu(h)

        if self.scale_mu:
            mu = scale_by_tanh(mu, high=self.action_space.high,
                               low=self.action_space.low)

        mat_diag = F.exp(self.mat_diag(h))
        if hasattr(self, 'mat_non_diag'):
            mat_non_diag = self.mat_non_diag(h)
            tril = lower_triangular_matrix(mat_diag, mat_non_diag)
            mat = F.batch_matmul(tril, tril, transb=True)
        else:
            mat = F.expand_dims(mat_diag ** 2, axis=2)
        return ContinuousQOutput(mu, mat, v, min_action=self.action_space.low,
                                 max_action=self.action_space.high)


class FCBNSIContinuousQFunction(chainer.Chain, QFunction):
    """Fully-connected state-input continuous Q-function."""

    def __init__(self, n_input_channels, n_dim_action, n_hidden_channels,
                 n_hidden_layers, action_space, scale_mu=True,
                 normalize_input=True):
        """
        Args:
          n_input_channels: number of input channels
          n_dim_action: number of dimensions of action space
          n_hidden_channels: number of hidden channels
          n_hidden_layers: number of hidden layers
          action_space: action_space
          scale_mu (bool): scale mu by applying tanh if True
        """

        self.n_input_channels = n_input_channels
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels

        assert action_space is not None
        self.scale_mu = scale_mu
        self.action_space = action_space

        layers = {}

        assert n_hidden_layers >= 1
        layers['hidden_layers'] = MLPBN(
              in_size=n_input_channels, out_size=n_hidden_channels,
              hidden_sizes=[n_hidden_channels] * (n_hidden_layers - 1),
              normalize_input=normalize_input)

        layers['v'] = L.Linear(n_hidden_channels, 1)
        layers['mu'] = L.Linear(n_hidden_channels, n_dim_action)
        layers['mat_diag'] = L.Linear(n_hidden_channels, n_dim_action)
        non_diag_size = n_dim_action * (n_dim_action - 1) // 2
        if non_diag_size > 0:
            layers['mat_non_diag'] = L.Linear(n_hidden_channels, non_diag_size)

        super().__init__(**layers)

    def __call__(self, state, test=False):
        xp = cuda.get_array_module(state)
        h = self.hidden_layers(state, test=test)
        v = self.v(h)
        mu = self.mu(h)

        if self.scale_mu:
            mu = scale_by_tanh(mu, high=self.action_space.high,
                               low=self.action_space.low)

        mat_diag = F.exp(self.mat_diag(h))
        if hasattr(self, 'mat_non_diag'):
            mat_non_diag = self.mat_non_diag(h)
            tril = lower_triangular_matrix(mat_diag, mat_non_diag)
            mat = F.batch_matmul(tril, tril, transb=True)
        else:
            mat = F.expand_dims(mat_diag ** 2, axis=2)
        return ContinuousQOutput(mu, mat, v, min_action=self.action_space.low,
                                 max_action=self.action_space.high)


class FCSAQFunction(chainer.ChainList, QFunction):
    """Fully-connected (s,a)-input continuous Q-function."""

    def __init__(self, n_dim_obs, n_dim_action, n_hidden_channels,
                 n_hidden_layers):
        """
        Args:
          n_dim_obs: number of dimensions of observation space
          n_dim_action: number of dimensions of action space
          n_hidden_channels: number of hidden channels
          n_hidden_layers: number of hidden layers
        """

        self.n_input_channels = n_dim_obs + n_dim_action
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels

        layers = []
        assert self.n_hidden_layers >= 1
        layers.append(
            L.Linear(self.n_input_channels, self.n_hidden_channels))
        for i in range(self.n_hidden_layers - 1):
            layers.append(
                L.Linear(self.n_hidden_channels, self.n_hidden_channels))
        layers.append(L.Linear(self.n_hidden_channels, 1))
        super().__init__(*layers)
        self.output = layers[-1]

    def __call__(self, state, action, test=False):
        h = F.concat((state, action), axis=1)
        for layer in self[:-1]:
            h = F.relu(layer(h))
        h = self[-1](h)
        return h


class FCBNSAQFunction(MLPBN, QFunction):
    """Fully-connected (s,a)-input continuous Q-function."""

    def __init__(self, n_dim_obs, n_dim_action, n_hidden_channels,
                 n_hidden_layers, normalize_input=True):
        """
        Args:
          n_dim_obs: number of dimensions of observation space
          n_dim_action: number of dimensions of action space
          n_hidden_channels: number of hidden channels
          n_hidden_layers: number of hidden layers
        """

        self.n_input_channels = n_dim_obs + n_dim_action
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels
        self.normalize_input = normalize_input
        super().__init__(
            in_size=self.n_input_channels, out_size=1,
            hidden_sizes=[self.n_hidden_channels] * self.n_hidden_layers,
            normalize_input=self.normalize_input)

    def __call__(self, state, action, test=False):
        h = F.concat((state, action), axis=1)
        return super().__call__(h, test=test)


class FCBNLateActionSAQFunction(chainer.Chain, QFunction):
    """Fully-connected (s,a)-input continuous Q-function.
    
    Actions are not included until the second hidden layer and not normalized.
    This architecture is used in the DDPG paper:
    http://arxiv.org/abs/1509.02971
    """

    def __init__(self, n_dim_obs, n_dim_action, n_hidden_channels,
                 n_hidden_layers, normalize_input=True):
        """
        Args:
          n_dim_obs: number of dimensions of observation space
          n_dim_action: number of dimensions of action space
          n_hidden_channels: number of hidden channels
          n_hidden_layers: number of hidden layers
        """

        self.n_input_channels = n_dim_obs + n_dim_action
        self.n_hidden_layers = n_hidden_layers
        self.n_hidden_channels = n_hidden_channels
        self.normalize_input = normalize_input

        super().__init__(
            obs_mlp=MLPBN(in_size=n_dim_obs, out_size=n_hidden_channels,
                          hidden_sizes=[], normalize_input=normalize_input,
                          normalize_output=True),
            mlp=MLP(in_size=n_hidden_channels + n_dim_action,
                    out_size=1,
                    hidden_sizes=[self.n_input_channels] * (self.n_hidden_layers - 1)))
        self.output = self.mlp.output

    def __call__(self, state, action, test=False):
        h = F.relu(self.obs_mlp(state, test=test))
        h = F.concat((h, action), axis=1)
        return self.mlp(h, test=test)
