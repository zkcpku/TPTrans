from torch import nn
from .embedding import LeftEmbedding, RightEmbedding, PathEmbedding
from .encoder import Encoder
import torch
import math
import torch.nn.functional as F


class Model(nn.Module):
    def __init__(self, args, s_vocab, t_vocab):
        super().__init__()
        self.args = args
        self.left_embedding = LeftEmbedding(args, s_vocab)
        self.right_embedding = RightEmbedding(args, t_vocab)
        if args.relation_path or args.absolute_path:
            self.path_embedding = PathEmbedding(args)
        # self.decoder_layer = nn.TransformerDecoderLayer(d_model=args.hidden, nhead=args.attn_heads,
        #                                                 dim_feedforward=args.d_ff_fold * args.hidden,
        #                                                 dropout=args.dropout, activation=self.args.activation)
        # self.decoder = nn.TransformerDecoder(self.decoder_layer, num_layers=args.decoder_layers)
        self.decoder = nn.Linear(args.hidden, args.clf_num)
        self.encoder = Encoder(args)
        self.softmax = nn.LogSoftmax(dim=-1)
        self.relation_path = args.relation_path
        self.absolute_path = args.absolute_path
        if self.args.uni_vocab:
            self.right_embedding.embedding.weight = self.left_embedding.embedding.weight
            if args.embedding_size != args.hidden:
                self.right_embedding.in_.weight = self.left_embedding.in_.weight
        if self.args.weight_tying:
            self.right_embedding.out.weight = self.right_embedding.embedding.weight
        if self.args.pointer:
            self.query_linear = nn.Linear(self.args.hidden, self.args.hidden)
            self.sentinel = nn.Parameter(torch.rand(1, self.args.hidden))
            if self.args.pointer_res:
                self.drop = nn.Dropout(p=self.args.dropout)
            if self.args.activation == 'gelu':
                self.activation = torch.nn.GELU()
            elif self.args.activation == 'relu':
                self.activation = torch.nn.ReLU()
            if self.args.pointer_type == 'add':
                self.additive_attention_W = nn.Linear(self.args.hidden * 2, self.args.hidden)
                self.additive_attention_v = nn.Parameter(torch.rand(self.args.hidden))

    def encode(self, data):
        content = data['content']
        content_mask = data['content_mask']
        path_map = data['path_map']
        paths = data['paths']
        paths_mask = data['paths_mask']
        r_paths = data['r_paths']
        r_paths_mask = data['r_paths_mask']
        r_path_idx = data['r_path_idx']
        named = data['named']

        content_ = self.left_embedding(content, named)
        if self.relation_path:
            paths_ = self.path_embedding(paths, paths_mask, type='relation')
        else:
            paths_ = None
        if self.absolute_path:
            r_paths_ = self.path_embedding(r_paths, r_paths_mask, type='absolute')
        else:
            r_paths_ = None
        mask_ = (content_mask > 0).unsqueeze(1).repeat(1, content_mask.size(1), 1).unsqueeze(1)
        # bs, 1,max_code_length,max_code_length

        memory = self.encoder(content_, mask_, paths_, path_map, r_paths_, r_path_idx)
        # bs, max_code_length, hidden
        return memory, (content_mask == 0)

    def pointer(self, out, feature, memory, memory_key_padding_mask, content_e, voc_len):
        voc_len = torch.max(voc_len).item()
        bs, src_len, tgt_len = memory.shape[0], memory.shape[1], feature.shape[1]
        pointer_key = torch.cat((memory, self.sentinel.unsqueeze(0).expand(bs, -1, -1)), dim=1)  # bs,src_len,hid
        pointer_query = self.activation((self.query_linear(feature)))  # bs,tgt,hid
        if self.args.pointer_res:
            pointer_query = self.drop(pointer_query) + feature
        if self.args.pointer_type == 'mul':
            pointer_atten = torch.einsum('bth,bsh->bts', pointer_query, pointer_key) / math.sqrt(self.args.hidden)
        elif self.args.pointer_type == 'add':
            pointer_query = pointer_query.unsqueeze(2).repeat(1, 1, pointer_key.shape[1], 1)  # bs,tgt,src_len,hid
            pointer_key = pointer_key.unsqueeze(1).repeat(1, tgt_len, 1, 1)  # bs,tgt,src_len,hid
            pointer_atten = self.activation(
                self.additive_attention_W(torch.cat([pointer_query, pointer_key], dim=-1)))  # bs,tgt,src_len,hid
            pointer_atten = torch.einsum('btsh,h->bts', pointer_atten, self.additive_attention_v)  # bs,tgt,src_len
        else:
            pointer_atten = None
        mask = torch.cat((memory_key_padding_mask, torch.ones(bs, 1).to(memory_key_padding_mask.device) == 0),
                         dim=-1).unsqueeze(1)  # bs,1,s
        pointer_atten = pointer_atten.masked_fill(mask, -1e9)
        pointer_atten = F.log_softmax(pointer_atten, dim=-1)
        pointer_gate = pointer_atten[:, :, -1].unsqueeze(-1)  # b,t,1
        pointer_atten = pointer_atten[:, :, :-1]  # b,t,s
        M = torch.zeros((bs, voc_len, src_len))
        # print(content_e.shape)
        # print(bs, voc_len, src_len)
        M[torch.arange(bs).unsqueeze(-1).expand(bs, src_len).reshape(-1),
          content_e.view(-1),
          torch.arange(src_len).repeat(bs)] = 1
        pointer_atten_p = torch.einsum('bts,bvs->btv', pointer_atten.exp(), M.to(pointer_atten.device))
        pointer_atten_log = (pointer_atten_p + torch.finfo(torch.float).eps).log()
        pointer_atten_log = pointer_atten_log - torch.log1p(
            -pointer_gate.exp() + torch.finfo(torch.float).eps)  # norm
        # pointer_atten_log: bs,max_target_len,extend_vocab_size. extend_vocab_size >= vocab_size
        # Avoid having -inf in attention scores as they produce NaNs during backward pass
        pointer_atten_log[pointer_atten_log == float('-inf')] = torch.finfo(torch.float).min
        if torch.isnan(pointer_atten_log).any():
            print("NaN in final pointer attention!", pointer_atten_log)

        out = torch.cat((out, torch.zeros(bs, tgt_len, voc_len - out.shape[-1]).fill_(float('-inf')).to(out.device)),
                        dim=-1)  # not 0 , should -inf

        p = torch.stack(
            [out + pointer_gate, pointer_atten_log + (1 - pointer_gate.exp() + torch.finfo(torch.float).eps).log()],
            dim=-2)  # bs,tgt_len,2,extend_voc
        out = torch.logsumexp(p, dim=-2)
        return out

    def decode(self, memory, f_source, memory_key_padding_mask, content_e=None, voc_len=None):
        '''
        :param voc_len: a list of voc len
        :param content_e: extended vocab mapped content for pointer
        :param memory_key_padding_mask
        :param memory: # bs, max_code_length, hidden
        :param f_source: # bs,max_target_len
        :return:
        '''
        f_source_ = self.right_embedding(f_source)
        f_len = f_source.shape[-1]
        tgt_mask = (torch.ones(f_len, f_len).tril_() == 0).to(memory.device)
        memory_key_padding_mask = memory_key_padding_mask.to(memory.device)
        tgt_key_padding_mask = (f_source == 0).to(memory.device)
        feature = self.decoder(f_source_.permute(1, 0, 2), memory.permute(1, 0, 2), tgt_mask=tgt_mask,
                               tgt_key_padding_mask=tgt_key_padding_mask,
                               memory_key_padding_mask=memory_key_padding_mask)

        feature = feature.permute(1, 0, 2)

        out = self.softmax(self.right_embedding.prob(feature))
        if self.args.pointer:
            out = self.pointer(out, feature, memory, memory_key_padding_mask, content_e, voc_len)
        return out

    def forward(self, data):
        f_source = data['f_source']
        memory, memory_key_padding_mask = self.encode(data)
        # if self.args.pointer:
        #     out = self.decode(memory, f_source, memory_key_padding_mask, data['content_e'], data['voc_len'])
        # else:
        #     out = self.decode(memory, f_source, memory_key_padding_mask)
        memory = memory[:, 0, :]
        out = self.decoder(memory)
        return out


