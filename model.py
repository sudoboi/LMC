import convlstm

import torch
import torch.nn as nn
from torch.nn import functional as F

import copy


class Predictor(nn.Module):
    def __init__(self, args):
        super(Predictor, self).__init__()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.encoder = nn.Sequential(
            nn.Conv2d(in_channels=1, out_channels=64, kernel_size=(3, 3), stride=2, padding=1),
            nn.ELU(),
            nn.Conv2d(in_channels=64, out_channels=64, kernel_size=(3, 3), stride=1, padding=1),
            nn.ELU(),
            nn.Conv2d(in_channels=64, out_channels=128, kernel_size=(3, 3), stride=2, padding=1),
            nn.ELU(),
            nn.Conv2d(in_channels=128, out_channels=128, kernel_size=(3, 3), stride=1, padding=1),
            nn.ELU())
        self.decoder = nn.Sequential(
            nn.ConvTranspose2d(in_channels=256, out_channels=128, kernel_size=(3, 3), stride=1, padding=1, output_padding=0),
            nn.ELU(),
            nn.ConvTranspose2d(in_channels=128, out_channels=64, kernel_size=(3, 3), stride=2, padding=1, output_padding=1),
            nn.ELU(),
            nn.ConvTranspose2d(in_channels=64, out_channels=64, kernel_size=(3, 3), stride=1, padding=1, output_padding=0),
            nn.ELU(),
            nn.ConvTranspose2d(in_channels=64, out_channels=1, kernel_size=(3, 3), stride=2, padding=1, output_padding=1))

        if args.dataset == 'kth':
            self.decoder.add_module("last_activation", nn.Sigmoid())

        self.convlstm_num = 4
        self.convlstm_in_c = [128, 128, 128, 128]
        self.convlstm_out_c = [128, 128, 128, 128]
        self.convlstm_list = []
        for layer_i in range(self.convlstm_num):
            self.convlstm_list.append(convlstm.NPUnit(in_channels=self.convlstm_in_c[layer_i],
                                                      out_channels=self.convlstm_out_c[layer_i],
                                                      kernel_size=[3, 3]))
        self.convlstm_list = nn.ModuleList(self.convlstm_list)
        
        self.memory = Memory(args.memory_size)

        self.attention_size = 128
        self.attention_func = nn.Sequential(
            nn.AdaptiveAvgPool2d([1, 1]),
            nn.Flatten(),
            nn.Linear(256, 16),
            nn.ReLU(),
            nn.Linear(16, self.attention_size),
            nn.Sigmoid())

    def forward(self, short_x, long_x, out_len, phase):
        batch_size = short_x.size()[0]
        input_len= short_x.size()[1]

        # long-term motion context recall
        memory_x = long_x if phase == 1 else short_x
        memory_feature = self.memory(memory_x, phase)

        # motion context-aware video prediction
        h, c, out_pred = [], [], []
        for layer_i in range(self.convlstm_num):
            zero_state = torch.zeros(batch_size, self.convlstm_in_c[layer_i], memory_feature.size()[2], memory_feature.size()[3]).to(self.device)
            h.append(zero_state)
            c.append(zero_state)
        for seq_i in range(input_len+out_len-1):
            if seq_i < input_len:
                input_x = short_x[:, seq_i, :, :, :]
                input_x = self.encoder(input_x)
            else:
                input_x = self.encoder(out_pred[-1])

            for layer_i in range(self.convlstm_num):
                if layer_i == 0:
                    h[layer_i], c[layer_i] = self.convlstm_list[layer_i](input_x, h[layer_i], c[layer_i])
                else:
                    h[layer_i], c[layer_i] = self.convlstm_list[layer_i](h[layer_i-1], h[layer_i], c[layer_i])

            if seq_i >= input_len-1:
                attention = self.attention_func(torch.cat([c[-1], memory_feature], dim=1))
                attention = torch.reshape(attention, (-1, self.attention_size, 1, 1))
                memory_feature_att = memory_feature * attention
                out_pred.append(self.decoder(torch.cat([h[-1], memory_feature_att], dim=1)))

        out_pred = torch.stack(out_pred)
        out_pred = out_pred.transpose(dim0=0, dim1=1)
        out_pred = out_pred[:, -out_len:, :, :, :]

        return out_pred


class Memory(nn.Module):
    def __init__(self, memory_size):
        super(Memory, self).__init__()
        self.motion_matching_encoder = nn.Sequential(
            nn.Conv3d(1, 64, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.MaxPool3d(kernel_size=(1, 2, 2), stride=(1, 2, 2)),
            nn.Conv3d(64, 128, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.MaxPool3d(kernel_size=(2, 2, 2), stride=(2, 2, 2)),
            nn.Conv3d(128, 256, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.Conv3d(256, 256, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.MaxPool3d(kernel_size=(2, 2, 2), stride=(2, 2, 2)),
            nn.Conv3d(256, 512, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.Conv3d(512, 512, kernel_size=(3, 3, 3), padding=(1, 1, 1)),
            nn.ReLU(),
            nn.MaxPool3d(kernel_size=(2, 2, 2), stride=(2, 2, 2)),
            nn.AdaptiveAvgPool3d([1, None, None]))

        self.motion_context_encoder = copy.deepcopy(self.motion_matching_encoder)

        self.embedder = nn.Sequential(
            nn.ConvTranspose2d(in_channels=512, out_channels=256, kernel_size=(3, 3), stride=2, padding=1, output_padding=1),
            nn.ReLU(),
            nn.ConvTranspose2d(in_channels=256, out_channels=128, kernel_size=(3, 3), stride=2, padding=1, output_padding=1),
            nn.ReLU())

        self.memory_shape = [memory_size, 512]
        self.memory_w = nn.init.normal_(torch.empty(self.memory_shape), mean=0.0, std=1.0)
        self.memory_w = nn.Parameter(self.memory_w, requires_grad=True)

    def forward(self, memory_x, phase):
        memory_x = memory_x[:, 1:, :, :, :] - memory_x[:, :-1, :, :, :] # make difference frames

        memory_x = memory_x.transpose(dim0=1, dim1=2) # make (N, C, T, H, W) for 3D Conv
        motion_encoder = self.motion_context_encoder if phase == 1 else self.motion_matching_encoder
        memory_query = torch.squeeze(motion_encoder(memory_x), dim=2) # make (N, C, H, W)

        query_c, query_h, query_w = memory_query.size()[1], memory_query.size()[2], memory_query.size()[3]
        memory_query = memory_query.permute(0, 2, 3, 1) # make (N, H, W, C)
        memory_query = torch.reshape(memory_query, (-1, query_c)) # make (N*H*W, C)

        # memory addressing
        query_norm = F.normalize(memory_query, dim=1)
        memory_norm = F.normalize(self.memory_w, dim=1)
        s = torch.mm(query_norm, memory_norm.transpose(dim0=0, dim1=1))
        addressing_vec = F.softmax(s, dim=1)
        memory_feature = torch.mm(addressing_vec, self.memory_w)

        memory_feature = torch.reshape(memory_feature, (-1, query_h, query_w, query_c)) # make (N, H, W, C)
        memory_feature = memory_feature.permute(0, 3, 1, 2) # make (N, C, H, W) for 2D DeConv
        memory_feature = self.embedder(memory_feature)

        return memory_feature
