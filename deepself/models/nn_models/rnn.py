from __future__ import print_function, division

from collections import OrderedDict

import torch
import torch.nn as nn

from deepself.models.nn_models.nn_utils import initialize_weights

supported_rnns = {
    'lstm': nn.LSTM,
    'rnn': nn.RNN,
    'gru': nn.GRU
}


from dataclasses import dataclass
from deepself.utils.enums import RNNType
from deepself.utils.nn_config import NNModelConfig


@dataclass
class RNNConfig(NNModelConfig):    # RNN model arguments
    # TODO remove "rnn_"
    rnn_type: RNNType = RNNType.gru     # Type of the RNN. rnn|gru|lstm|deepspeech are supported
    rnn_hidden_size: int = 100      # Hidden size of RNNs
    rnn_n_layers: int = 1  # Number of RNN layers
    max_norm: int = 400     # Norm cutoff to prevent explosion of gradients
    bidirectional: bool = True      # Turn off bi-directional RNNs, introduces lookahead convolution
    # TODO change to bn
    batch_norm_size: int = 0   # Batch normalization or not
    seq_len: int = 0  # Length of sequence


def construct_rnn(cfg, output_size):
    """

    :param cfg: {
        'rnn_type': 'deepspeech' or lstm or gru,
        'input_size': input feature size of data
        'n_layers': Number of layers in rnn
        'seq_len': Length of time dimension
        'hidden_size': Number of hidden size in rnn
        'is_bidirectional': True or False
        'is_inference_softmax': True or False
    }
    :return:
    """
    if len(cfg.input_size) == 2:
        cfg.input_size = cfg.input_size[0]

    return RNNClassifier(cfg.input_size, out_time_feature=cfg.seq_len,
                         rnn_type=supported_rnns[cfg.rnn_type.value], output_size=output_size,
                         rnn_hidden_size=cfg.rnn_hidden_size, n_layers=cfg.rnn_n_layers,
                         bidirectional=cfg.bidirectional, batch_norm_size=cfg.get('batch_norm_size'))


class SequenceWise(nn.Module):
    def __init__(self, module):
        """
        Collapses input of dim T*N*H to (T*N)*H, and applies to a module.
        Allows handling of variable sequence lengths and minibatch sizes.
        :param module: Module to apply input to.
        """
        super(SequenceWise, self).__init__()
        self.module = module

    def forward(self, x):
        t, n = x.size(0), x.size(1)
        # if sequence-wise, normalize at last dimension, should be n x t direction
        x = x.reshape(t * n, -1)   # t x n x h -> (t x n) x h
        x = self.module(x)
        x = x.view(t, n, -1)
        return x

    def __repr__(self):
        tmpstr = self.__class__.__name__ + ' (\n'
        tmpstr += self.module.__repr__()
        tmpstr += ')'
        return tmpstr


class BatchRNN(nn.Module):
    def __init__(self, input_size, hidden_size, batch_norm_size, sequence_wise=False, rnn_type=nn.LSTM,
                 bidirectional=False):
        super(BatchRNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.batch_norm = SequenceWise(nn.BatchNorm1d(batch_norm_size)) if sequence_wise else nn.BatchNorm1d(batch_norm_size)
        self.rnn = initialize_weights(
            rnn_type(input_size=input_size, hidden_size=hidden_size, bidirectional=bidirectional, bias=True))
        self.num_directions = 2 if bidirectional else 1

    def forward(self, x):
        x = x.transpose(0, 1).transpose(1, 2)   # l x n x c -> n x c x l
        x = self.batch_norm(x.to(torch.float))
        x = x.transpose(1, 2).transpose(0, 1)  # n x c x l -> l x n x h
        x, _ = self.rnn(x)

        if self.bidirectional:
            x = x.view(x.size(0), x.size(1), 2, -1).sum(dim=2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum

        return x


class RNN(nn.Module):
    def __init__(self, input_size, hidden_size, batch_norm_size, sequence_wise=False, rnn_type=nn.LSTM,
                 bidirectional=False):
        super(RNN, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.rnn = initialize_weights(
            rnn_type(input_size=input_size, hidden_size=hidden_size, bidirectional=bidirectional, bias=True))
        self.num_directions = 2 if bidirectional else 1

    def forward(self, x):
        x, _ = self.rnn(x.to(torch.float))

        if self.bidirectional:
            x = x.view(x.size(0), x.size(1), 2, -1).sum(dim=2).view(x.size(0), x.size(1), -1)  # (TxNxH*2) -> (TxNxH) by sum
        return x


class InferenceBatchSoftmax(nn.Module):
    def forward(self, input_):
        if not self.training:
            return torch.exp(nn.LogSoftmax(dim=-1)(input_))
        else:
            return input_


class RNNClassifier(nn.Module):
    def __init__(self, input_size, out_time_feature, output_size, batch_norm_size=None, sequence_wise=False,
                 rnn_type=nn.LSTM, rnn_hidden_size=768, n_layers=5, bidirectional=True):
        super(RNNClassifier, self).__init__()

        rnns = []
        rnn_cls = BatchRNN if batch_norm_size else RNN
        rnn = rnn_cls(input_size=input_size, hidden_size=rnn_hidden_size, rnn_type=rnn_type,
                       bidirectional=bidirectional, batch_norm_size=batch_norm_size, sequence_wise=sequence_wise)
        rnns.append(('0', rnn))
        for x in range(n_layers - 1):
            rnn = rnn_cls(input_size=rnn_hidden_size, hidden_size=rnn_hidden_size,
                          rnn_type=rnn_type, bidirectional=bidirectional, batch_norm_size=rnn_hidden_size,
                          sequence_wise=sequence_wise)
            rnns.append(('%d' % (x + 1), rnn))
        self.rnns = nn.Sequential(OrderedDict(rnns))
        self.fc = nn.Sequential(
            nn.BatchNorm1d(rnn_hidden_size * out_time_feature),
            # nn.BatchNorm1d(26500),
            # nn.BatchNorm1d(rnn_hidden_size),
            initialize_weights(nn.Linear(rnn_hidden_size * out_time_feature, output_size, bias=False))
            # initialize_weights(nn.Linear(26500, output_size, bias=False))
            # initialize_weights(nn.Linear(rnn_hidden_size, output_size, bias=False))
        )
        self.classify = True if output_size != 1 else False

    def forward(self, x):
        x = x.transpose(0, 2).transpose(1, 2)    # batch x feature x time -> # time x batch x feature

        for rnn in self.rnns:
            x = rnn(x)

        x = x.transpose(0, 1)   # time x batch x freq -> batch x time x freq

        x = x.transpose(1, 2)  # batch x sequence x freq -> batch x freq x sequence
        # x = nn.AvgPool1d(kernel_size=x.size(2))(x)  # average within sequence, outputs batch x freq x 1
        x = x.reshape(x.size(0), -1)

        x = self.fc(x)

        if not self.classify:
            return x

        x = torch.exp(nn.LogSoftmax(dim=-1)(x))

        return x


class DeepSpeech(RNNClassifier):
    def __init__(self, conv, input_size, out_time_feature, rnn_type=nn.LSTM, rnn_hidden_size=768, n_layers=5,
                 bidirectional=True, output_size=2):
        super(DeepSpeech, self).__init__(input_size=input_size, out_time_feature=out_time_feature, rnn_type=nn.LSTM,
                                         rnn_hidden_size=rnn_hidden_size, n_layers=n_layers,
                                         bidirectional=bidirectional, output_size=output_size, batch_norm_size=input_size)

        self.hidden_size = rnn_hidden_size
        self.hidden_layers = n_layers
        self.rnn_type = rnn_type
        self.bidirectional = bidirectional

        self.conv = conv
        print(f'Number of parameters\tconv: {get_param_size(self.conv)}\trnn: {get_param_size(super())}')

    def forward(self, x):
        x = self.conv(x.to(torch.float))    # batch x channel x freq x time

        sizes = x.size()    # batch x channel x freq_feature x time_feature
        if len(sizes) == 4:
            x = x.view(sizes[0], sizes[1] * sizes[2], sizes[3])  # Collapse feature dimension   batch x feature x time
        x = super().forward(x)
        return x

    def change_last_layer(self, n_classes):
        self.fc[1] = initialize_weights(nn.Linear(self.fc[1].in_features, n_classes, bias=False))
        # print(self.fc[1].in_features)
        # self.fc[1] = nn.Linear(self.fc[1].in_features, n_classes, bias=False)
