import torch.nn as nn

from .attention import MultiHeadedAttention
from .utils import SublayerConnection, PositionwiseFeedForward
import torch


class TransformerBlock(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.attention = MultiHeadedAttention(args)
        self.feed_forward = PositionwiseFeedForward(args)
        self.input_sublayer = SublayerConnection(args)
        self.output_sublayer = SublayerConnection(args)
        self.dropout = nn.Dropout(p=args.dropout)

    def forward(self, content, mask, r_k, r_v, path_map, ap):
        '''
        :param ap: bs,h,max_code_length,max_code_length
        :param path_map: bs,max_code_length,max_code_length
        :param content: bs, max_code_length, hidden
        :param r_k: bs, h,max_path_num, hidden//heads
        :param r_v: bs, h,max_path_num, hidden//heads
        :param mask: bs, 1,max_code_length,max_code_length
        :return:
        '''
        x = self.input_sublayer(content,
                                lambda _x: self.attention.forward(_x, _x, _x, mask=mask, r_k=r_k, r_v=r_v,
                                                                  path_map=path_map, ap=ap))
        x = self.output_sublayer(x, self.feed_forward)
        return self.dropout(x)


class Encoder(nn.Module):

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.transformer_blocks = nn.ModuleList([TransformerBlock(args) for _ in range(args.layers)])
        self.max_code_length = args.max_code_length
        # self.relative_position = args.relative_position
        self.h = args.attn_heads
        self.absolute_path = args.absolute_path
        self.relative_path = args.relation_path
        if self.args.ap_kq:
            out_size = self.args.hidden // self.args.attn_heads
            self.ap_k = nn.Linear(self.args.hidden // self.args.attn_heads, out_size)
            self.ap_q = nn.Linear(self.args.hidden // self.args.attn_heads, out_size)
        if self.args.rp_kv:
            self.rp_k = nn.Linear(self.args.hidden // self.args.attn_heads, self.args.hidden // self.args.attn_heads)
            self.rp_v = nn.Linear(self.args.hidden // self.args.attn_heads, self.args.hidden // self.args.attn_heads)

    def forward(self, content, mask, paths, path_map, r_paths_, r_path_idx):
        '''
        :param r_paths_: bs,max_path_num,hidden
        :param r_path_idx: bs,max_code_length
        :param content: bs, max_code_length, hidden
        :param paths: bs,max_path_num,hidden
        :param mask: bs, 1,max_code_length,max_code_length
        :param path_map: bs,max_code_length,max_code_length
        :return:
        '''
        if self.relative_path:
            r_k = paths.unsqueeze(1).expand(-1, self.h, -1, -1)
            r_v = paths.unsqueeze(1).expand(-1, self.h, -1, -1)
            if self.args.rp_kv:
                r_k = self.rp_k(r_k)
                r_v = self.rp_v(r_v)
        else:
            r_k = None
            r_v = None

        if self.absolute_path:
            abs_path = torch.cat((r_paths_, torch.zeros(r_paths_.shape[0], 1, r_paths_.shape[-1]).to(r_paths_.device)),
                                 dim=1).gather(1, r_path_idx.unsqueeze(-1).expand(-1, -1, r_paths_.shape[-1]))
            # bs,max_code_length,hidden//head
            if self.args.ap_kq:
                ap = torch.einsum('abc,adc->abd', self.ap_k(abs_path), self.ap_q(abs_path)) \
                    .unsqueeze(1).expand(-1, self.h, -1, -1)
            else:
                ap = torch.einsum('abc,adc->abd', abs_path, abs_path).unsqueeze(1).expand(-1, self.h, -1, -1)
        else:
            ap = None

        for transformer in self.transformer_blocks:
            content = transformer(content, mask, r_k, r_v, path_map, ap)
        return content
