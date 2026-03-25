# test_verify.py
import binascii
import nacl.signing

def test_signature():
    # 官方示例
    bot_secret = "DG5g3B4j9X2KOErG"
    event_ts = "1725442341"
    plain_token = "Arq0D5A61EgUu4OxUvOp"
    expected = "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"

    # 扩展 seed 到 32 字节
    seed = bot_secret
    while len(seed) < 32:
        seed += bot_secret
    seed_bytes = seed[:32].encode("utf-8")

    # 生成签名
    signing_key = nacl.signing.SigningKey(seed_bytes)
    message = (event_ts + plain_token).encode("utf-8")
    signed = signing_key.sign(message)
    signature = binascii.hexlify(signed.signature).decode("utf-8")

    print(f"计算签名: {signature}")
    print(f"期望签名: {expected}")
    print(f"匹配: {signature == expected}")

test_signature()
