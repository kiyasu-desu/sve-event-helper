"""算出 mitmproxy CA 证书的 OpenSSL subject_hash_old 与 DER，供 Android 系统证书库使用。

Android 的系统证书库按 OpenSSL 老算法命名文件：<subject_hash_old>.0
subject_hash_old = MD5(canonical DER of X509_NAME)[:4] 按小端读成 8 位十六进制。
注意这与 pyOpenSSL 的 X509Name.hash()（新算法 hash）结果不同，不能混用。

用法: python _tools/ca_hash.py <mitmproxy-ca-cert.pem> <输出.der>
输出: 第一行 = subject_hash_old
      第二行 = subject (RFC4514)
      第三行 = 交叉验证信息
"""
import hashlib
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization

# 常见的 X.520 属性 OID（cryptography 48 起 ObjectIdentifier 不再暴露 dump()）
OID_DER = {
    "2.5.4.3": bytes([0x06, 0x03, 0x55, 0x04, 0x03]),    # commonName
    "2.5.4.4": bytes([0x06, 0x03, 0x55, 0x04, 0x04]),    # surname
    "2.5.4.5": bytes([0x06, 0x03, 0x55, 0x04, 0x05]),    # serialNumber
    "2.5.4.6": bytes([0x06, 0x03, 0x55, 0x04, 0x06]),    # countryName
    "2.5.4.7": bytes([0x06, 0x03, 0x55, 0x04, 0x07]),    # localityName
    "2.5.4.8": bytes([0x06, 0x03, 0x55, 0x04, 0x08]),    # stateOrProvinceName
    "2.5.4.9": bytes([0x06, 0x03, 0x55, 0x04, 0x09]),    # streetAddress
    "2.5.4.10": bytes([0x06, 0x03, 0x55, 0x04, 0x0A]),   # organizationName
    "2.5.4.11": bytes([0x06, 0x03, 0x55, 0x04, 0x0B]),   # organizationalUnitName
    "2.5.4.12": bytes([0x06, 0x03, 0x55, 0x04, 0x0C]),   # title
    "2.5.4.42": bytes([0x06, 0x03, 0x55, 0x04, 0x2A]),   # givenName
    "1.2.840.113549.1.9.1": bytes([0x06, 0x09, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x01]),  # emailAddress
    "0.9.2342.19200300.100.1.25": bytes([0x06, 0x0A, 0x09, 0x92, 0x26, 0x89, 0x93, 0xF2, 0x2C, 0x64, 0x01, 0x19]),  # domainComponent
}


def encode_oid_der(dotted: str) -> bytes:
    if dotted in OID_DER:
        return OID_DER[dotted]
    parts = [int(p) for p in dotted.split(".")]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = []
        while True:
            chunk.insert(0, p & 0x7F)
            p >>= 7
            if not p:
                break
        for i in range(len(chunk) - 1):
            chunk[i] |= 0x80
        body += bytes(chunk)
    return _tlv(0x06, body)


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def name_to_canonical_der(name: x509.Name) -> bytes:
    """i2d_X509_NAME 等价：每个 RDN 是一个 DER 排序的 SET OF AttributeTypeAndValue。"""
    rdns = []
    for rdn in name.rdns:
        atvs = []
        for atv in rdn:
            oid_der = encode_oid_der(atv.oid.dotted_string)
            value = atv.value
            raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
            atvs.append(_tlv(0x30, oid_der + _tlv(0x0C, raw)))
        atvs.sort()
        rdns.append(_tlv(0x31, b"".join(atvs)))
    return _tlv(0x30, b"".join(rdns))


def subject_hash_old(cert: x509.Certificate) -> str:
    der = name_to_canonical_der(cert.subject)
    hdr = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    digest = hashlib.md5(der[hdr:]).digest()
    return "%08x" % int.from_bytes(digest[:4], "little")


def known_answer_check() -> str:
    """参考值：OpenSSL 对 'O=mitmproxy,CN=mitmproxy' 的 subject_hash_old 已知为 1e1d5b1a。"""
    name = x509.Name([
        x509.NameAttribute(x509.oid.NameOID.ORGANIZATION_NAME, "mitmproxy"),
        x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "mitmproxy"),
    ])
    return subject_hash_old.__wrapped__(name) if hasattr(subject_hash_old, "__wrapped__") else _hash_of_name(name)


def _hash_of_name(name: x509.Name) -> str:
    der = name_to_canonical_der(name)
    hdr = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    return "%08x" % int.from_bytes(hashlib.md5(der[hdr:]).digest()[:4], "little")


def main() -> int:
    pem_path, der_path = sys.argv[1], sys.argv[2]
    pem = open(pem_path, "rb").read()
    cert = x509.load_pem_x509_certificate(pem)
    open(der_path, "wb").write(cert.public_bytes(serialization.Encoding.DER))

    ref = _hash_of_name(x509.Name([
        x509.NameAttribute(x509.oid.NameOID.ORGANIZATION_NAME, "mitmproxy"),
        x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "mitmproxy"),
    ]))
    mine = subject_hash_old(cert)

    note = []
    if ref == "1e1d5b1a":
        note.append("自检通过(O=mitmproxy,CN=mitmproxy -> 1e1d5b1a)")
    else:
        note.append("自检异常: 参考名哈希=%s (预期 1e1d5b1a)，请谨慎" % ref)
    try:
        from OpenSSL import crypto  # type: ignore
        x = crypto.load_certificate(crypto.FILETYPE_PEM, pem)
        note.append("pyopenssl new-hash=%08x (仅参考，与文件名无关)" % (x.get_subject().hash() & 0xFFFFFFFF))
    except Exception as exc:  # noqa: BLE001
        note.append("pyopenssl 不可用: %s" % exc)

    print(mine)
    print(cert.subject.rfc4514_string())
    print("; ".join(note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
