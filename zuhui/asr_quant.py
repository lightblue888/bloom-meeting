# -*- coding: utf-8 -*-
"""fp32 ONNX → 和原版一样的 int8（只量化 MatMul，权重 uint8，动态量化），元数据照抄原版的 encoder_adaptor。
在带 onnx 包的 Python 里跑（zuhui/asr_finetune.py 用 -I 起它），不 import zuhui。

  python -I asr_quant.py <fp32.onnx> <out.int8.onnx> <原版 encoder_adaptor.int8.onnx>
"""
import sys

import onnx
from onnxruntime.quantization import QuantType, quantize_dynamic


def main() -> int:
    src, dst, ref = sys.argv[1:4]
    quantize_dynamic(src, dst, op_types_to_quantize=["MatMul"], weight_type=QuantType.QUInt8, per_channel=False)
    m = onnx.load(dst)
    r = onnx.load(ref, load_external_data=False)
    keep = {p.key: p.value for p in r.metadata_props}       # sherpa-onnx 读这些（lfr_window_size 等）
    keep["comment"] = (keep.get("comment") or "") + " | adapted on the user's own speech (Bloom asr_train)"
    del m.metadata_props[:]
    for k, v in keep.items():
        p = m.metadata_props.add()
        p.key, p.value = k, v
    onnx.save(m, dst)
    print("ok", dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
