'''
This repository is used to implement all upsamplers(only x4) and tools for Efficient SR
@author
    LI Zehyuan from SIAT
    LIU yingqi from SIAT
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
from basicsr.utils.registry import ARCH_REGISTRY


class UpsampleOneStep(nn.Sequential):
    """UpsampleOneStep module (the difference with Upsample is that it always only has 1conv + 1pixelshuffle)
       Used in lightweight SR to save parameters.
    Args:
        scale (int): Scale factor. Supported scales: 2^n and 3.
        num_feat (int): Channel number of intermediate features.
    """

    def __init__(self, scale, num_feat, num_out_ch, input_resolution=None):
        self.num_feat = num_feat
        self.input_resolution = input_resolution
        m = []
        m.append(nn.Conv2d(num_feat, (scale**2) * num_out_ch, 3, 1, 1))
        m.append(nn.PixelShuffle(scale))
        super(UpsampleOneStep, self).__init__(*m)

    def flops(self):
        h, w = self.input_resolution
        flops = h * w * self.num_feat * 3 * 9
        return flops


class PixelShuffleDirect(nn.Module):
    def __init__(self, scale, num_feat, num_out_ch, input_resolution=None):
        super(PixelShuffleDirect, self).__init__()
        self.upsampleOneStep = UpsampleOneStep(scale, num_feat, num_out_ch, input_resolution=None)

    def forward(self, x):
        return self.upsampleOneStep(x)



class BSConvU(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1,
                 dilation=1, bias=True, padding_mode="zeros", with_ln=False, bn_kwargs=None):
        super().__init__()
        self.with_ln = with_ln
        # check arguments
        if bn_kwargs is None:
            bn_kwargs = {}

        # pointwise
        self.pw = nn.Linear(in_channels, out_channels)
        # self.pw=torch.nn.Conv2d(
        #         in_channels=in_channels,
        #         out_channels=out_channels,
        #         kernel_size=(1, 1),
        #         stride=1,
        #         padding=0,
        #         dilation=1,
        #         groups=1,
        #         bias=False,
        # )
        # batchnorm
        if with_ln:
            self.ln = torch.nn.LayerNorm(out_channels, **bn_kwargs)

        # depthwise
        self.dw = torch.nn.Conv2d(
                in_channels=out_channels,
                out_channels=out_channels,
                kernel_size=kernel_size,
                stride=stride,
                padding=padding,
                dilation=dilation,
                groups=out_channels,
                bias=bias,
                padding_mode=padding_mode,
        )

    def forward(self, fea):
        fea = fea.permute(0, 2, 3, 1)
        fea = self.pw(fea)
        if self.with_ln:
            fea = self.ln(fea)
        fea = self.dw(fea.permute(0, 3, 1, 2))
        return fea


class ESA(nn.Module):
    def __init__(self, num_feat=50, conv=nn.Conv2d, p=0.25):
        super(ESA, self).__init__()
        f = num_feat // 4
        BSConvS_kwargs = {}
        if conv.__name__ == 'BSConvS':
            BSConvS_kwargs = {'p': p}
        self.conv1 = nn.Linear(num_feat, f)
        self.conv_f = nn.Linear(f, f)
        self.maxPooling = nn.MaxPool2d(kernel_size=7, stride=3)
        self.conv_max = conv(f, f, kernel_size=3, **BSConvS_kwargs)
        self.conv2 = nn.Conv2d(f, f, 3, 2, 0)
        self.conv3 = conv(f, f, kernel_size=3, **BSConvS_kwargs)
        self.conv3_ = conv(f, f, kernel_size=3, **BSConvS_kwargs)
        self.conv4 = nn.Linear(f, num_feat)
        self.sigmoid = nn.Sigmoid()
        self.GELU = nn.GELU()

    def forward(self, input):
        x = input.permute(0, 2, 3, 1)
        c1_ = (self.conv1(x))
        c1 = self.conv2(c1_.permute(0, 3, 1, 2))
        v_max = self.maxPooling(c1)
        v_range = self.GELU(self.conv_max(v_max))
        c3 = self.GELU(self.conv3(v_range))
        c3 = self.conv3_(c3)
        c3 = F.interpolate(c3, (input.size(2), input.size(3)), mode='bilinear', align_corners=False)
        cf = self.conv_f(c1_)
        c4 = self.conv4((c3.permute(0, 2, 3, 1) + cf))
        m = self.sigmoid(c4.permute(0, 3, 1, 2))

        return input * m


class RFDB(nn.Module):
    def __init__(self, in_channels, out_channels, distillation_rate=0.25, conv=nn.Conv2d, p=0.25):
        super(RFDB, self).__init__()
        kwargs = {'padding': 1}
        if conv.__name__ == 'BSConvS':
            kwargs = {'p': p}

        self.dc = self.distilled_channels = in_channels // 2
        self.rc = self.remaining_channels = in_channels

        self.c1_d = nn.Linear(in_channels, self.dc)
        self.c1_r = conv(in_channels, self.rc, kernel_size=3,  **kwargs)
        self.c2_d = nn.Linear(self.remaining_channels, self.dc)
        self.c2_r = conv(self.remaining_channels, self.rc, kernel_size=3, **kwargs)
        self.c3_d = nn.Linear(self.remaining_channels, self.dc)
        self.c3_r = conv(self.remaining_channels, self.rc, kernel_size=3, **kwargs)

        self.c4 = conv(self.remaining_channels, self.dc, kernel_size=3, **kwargs)
        self.act = nn.GELU()

        self.c5 = nn.Linear(self.dc * 4, in_channels)
        self.esa = ESA(in_channels, conv)
        self.cw = nn.Parameter(torch.normal(mean=1, std=0.2, size=(1, in_channels)))
        self.conv_out = nn.Linear(in_channels, out_channels)

    def forward(self, input):

        distilled_c1 = self.act(self.c1_d(input.permute(0, 2, 3, 1)))
        r_c1 = (self.c1_r(input))
        r_c1 = self.act(r_c1 + input)

        distilled_c2 = self.act(self.c2_d(r_c1.permute(0, 2, 3, 1)))
        r_c2 = (self.c2_r(r_c1))
        r_c2 = self.act(r_c2 + r_c1)

        distilled_c3 = self.act(self.c3_d(r_c2.permute(0, 2, 3, 1)))
        r_c3 = (self.c3_r(r_c2))
        r_c3 = self.act(r_c3 + r_c2)

        r_c4 = self.act(self.c4(r_c3))

        out = torch.cat([distilled_c1, distilled_c2, distilled_c3, r_c4.permute(0, 2, 3, 1)], dim=3)
        out = self.c5(out).permute(0, 3, 1, 2)
        out_fused = self.esa(out)
        out_fused = out_fused.permute(0, 2, 3, 1) * self.cw
        out_fused = self.conv_out(out_fused)

        return out_fused.permute(0, 3, 1, 2) + input


def make_layer(block, n_layers):
    layers = []
    for _ in range(n_layers):
        layers.append(block())
    return nn.Sequential(*layers)


@ARCH_REGISTRY.register()
class RFDNFINALB5(nn.Module):
    def __init__(self, num_in_ch=3, num_feat=50, num_block=4, num_out_ch=3, upscale=4,
                 conv='BSConvU', upsampler='pixelshuffledirect', p=0.25):
        super(RFDNFINALB5, self).__init__()
        kwargs = {'padding': 1}
        if conv == 'BSConvS':
            kwargs = {'p': p}
        print(conv)
        if conv == 'BSConvU':
            self.conv = BSConvU
        else:
            self.conv = nn.Conv2d
        self.fea_conv = self.conv(num_in_ch * 4, num_feat, kernel_size=3, **kwargs)

        # RFDB_block_f = functools.partial(RFDB, in_channels=num_feat, conv=self.conv, p=p)
        # RFDB_trunk = make_layer(RFDB_block_f, num_block)
        self.B1 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        self.B2 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        self.B3 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        self.B4 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        self.B5 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        # self.B6 = RFDB(in_channels=num_feat, out_channels=num_feat, conv=self.conv, p=p)
        # self.B7 = RFDB(in_channels=num_feat, conv=self.conv, p=p)

        self.c1 = nn.Linear(num_feat * num_block, num_feat)
        self.GELU = nn.GELU()

        self.c2 = self.conv(num_feat, num_feat, kernel_size=3, **kwargs)


        if upsampler == 'pixelshuffledirect':
            self.upsampler = PixelShuffleDirect(scale=upscale, num_feat=num_feat, num_out_ch=num_out_ch)
        else:
            raise NotImplementedError(("Check the Upsampeler. None or not support yet"))

    def forward(self, input):
        input = torch.cat([input, input, input, input], dim=1)
        out_fea = self.fea_conv(input)
        out_B1 = self.B1(out_fea)
        out_B2 = self.B2(out_B1)
        out_B3 = self.B3(out_B2)
        out_B4 = self.B4(out_B3)
        out_B5 = self.B5(out_B4)
        # out_B6 = self.B6(out_B5)
        # out_B7 = self.B7(out_B6)
        trunk = torch.cat([out_B1, out_B2, out_B3, out_B4, out_B5], dim=1)
        out_B = self.c1(trunk.permute(0, 2, 3, 1))
        out_B = self.GELU(out_B.permute(0, 3, 1, 2))
        # print(out_B.shape)
        out_lr = self.c2(out_B) + out_fea

        # output = self.c3(out_lr)
        output = self.upsampler(out_lr)

        return output

