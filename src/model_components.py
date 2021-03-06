from typing import List, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence


class Embedder(nn.Module):
    def __init__(self,
                 vocab_size: int,
                 d_emb: int
                 ) -> None:
        super(Embedder, self).__init__()
        self.d_emb = d_emb
        self.embed = nn.Embedding(num_embeddings=vocab_size, embedding_dim=d_emb)

    def forward(self,
                x: torch.Tensor,    # (b, max_len)
                mask: torch.Tensor  # (b, max_len)
                ) -> torch.Tensor:
        x = x * mask
        embedded = self.embed(x)
        size = (-1, -1, self.d_emb)
        mask = mask.unsqueeze(-1).expand(size).type(embedded.dtype)
        embedded = embedded * mask  # PAD -> zero vector
        return embedded             # (b, max_len, d_emb)

    def set_initial_embedding(self,
                              initial_weight: np.array,
                              freeze: bool = True
                              ) -> None:
        self.embed.weight = nn.Parameter(torch.Tensor(initial_weight), requires_grad=(freeze is False))


class Encoder(nn.Module):
    def __init__(self,
                 rnn: nn.Module,
                 dropout_rate: float = 0.333
                 ) -> None:
        super(Encoder, self).__init__()
        self.rnn = rnn
        # self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self,
                x: torch.Tensor,     # (b, max_sou_len, d_emb)
                mask: torch.Tensor,  # (b, max_sou_len)
                ) -> Tuple[torch.Tensor, List[torch.Tensor]]:
        lengths = mask.sum(dim=1)    # (b)
        sorted_lengths, sorted_indices = lengths.sort(0, descending=True)
        sorted_input = x.index_select(0, sorted_indices)
        _, unsorted_indices = sorted_indices.sort(0)

        # masking
        packed = pack_padded_sequence(sorted_input, lengths=sorted_lengths, batch_first=True)
        output, states = self.rnn(packed, None)  # output: (sum(lengths), d_e_hid * n_dir)
        unpacked, _ = pad_packed_sequence(output, batch_first=True, padding_value=0)
        unsorted_output = unpacked.index_select(0, unsorted_indices)
        unsorted_states = [state.index_select(1, unsorted_indices) for state in states]
        # (b, max_sou_len, d_e_hid * n_dir), (n_lay * n_dir, b, d_e_hid)
        return unsorted_output, unsorted_states


class Decoder(nn.Module):
    def __init__(self,
                 rnn: nn.Module,
                 dropout_rate: float = 0.333
                 ) -> None:
        super(Decoder, self).__init__()
        self.rnn = rnn
        # self.dropout = nn.Dropout(p=dropout_rate)

    def forward(self,
                x: torch.Tensor,                           # (b, d_emb)
                mask: torch.Tensor,                        # (b, 1)
                states: Tuple[torch.Tensor, torch.Tensor]  # states[0]: (b, d_d_hid)
                ) -> Tuple[torch.Tensor, Tuple[torch.Tensor]]:
        valid_len = mask.sum()

        if valid_len > 0:
            sorted_lengths, sorted_indices = mask.sort(0, descending=True)
            sorted_input = x.index_select(0, sorted_indices)
            _, unsorted_indices = sorted_indices.sort(0)

            valid_input = sorted_input[:valid_len, :]
            if states is not None:
                cache = [state.index_select(0, sorted_indices) for state in states]
                old_states = [state[valid_len:, :].contiguous() for state in cache]
                states = [state[:valid_len, :].contiguous() for state in cache]
            new_states = self.rnn(valid_input, states)  # state: (valid_len, d_d_hid)

            b = x.size(0)

            if valid_len < b and states is not None:
                # (valid_len, d_d_hid) -> (b, d_d_hid)
                new_states = tuple([torch.cat((ns, os), dim=0) for ns, os in zip(new_states, old_states)])

            unsorted_states = tuple([new_state.index_select(0, unsorted_indices) for new_state in new_states])
            unsorted_output = unsorted_states[0]
        # all words are PAD or EOS
        else:
            # do not update hidden state
            unsorted_states = states
            unsorted_output = states[0]

        # (b, d_d_hid), ((b, d_d_hid), (b, d_d_hid))
        return unsorted_output, unsorted_states


class Maxout(nn.Module):  # concatenation and activation layer
    def __init__(self,
                 d_inp: int,
                 d_out: int,
                 pool_size: int
                 ) -> None:
        super().__init__()
        self.d_inp, self.d_out, self.pool_size = d_inp, d_out, pool_size
        self.w = nn.Linear(d_inp, d_out * pool_size)

    def forward(self,
                x: torch.Tensor
                ) -> torch.Tensor:
        size = list(x.size())
        size[-1] = self.d_out
        size.append(self.pool_size)
        max_dim = len(size) - 1
        out = self.w(x)
        y, _ = out.view(*size).max(max_dim)
        return y
