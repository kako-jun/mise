"""公開アドレスの判定(#130)。**判定の表はここ 1 か所だけ**。取得(fetcher.check_public_host)も、サムネイル撮影のフィルタ(scripts/thumbnail/netfilter.py)もこれを使う。
Python の is_global に揃え、is_global が真でも通さない範囲(IPv4 互換・非推奨の範囲・マルチキャスト)を加える。NAT64・6to4 は埋め込まれた IPv4 を検査する。
"""
import ipaddress

# is_global が true を返しても通さない範囲(IPv4 を埋め込む移行用アドレス・非推奨の範囲。マルチキャスト・未指定は is_public_address で除く)。
# is_global が既に false にする範囲(198.18/15・192.0.0/24・ドキュメント用・100.64/10・ループバック・リンクローカル・ULA 等)はここに重ねて書かない。
_NAT64 = ipaddress.ip_network('64:ff9b::/96')   # IPv4 を埋め込む(埋め込まれた側を検査する)
_6TO4 = ipaddress.ip_network('2002::/16')
_EXTRA_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    '::/96',          # IPv4 互換(::7f00:1 など。非推奨だが一部の実装は IPv4 として扱う)
    '64:ff9b:1::/48',
    'fec0::/10',      # サイトローカル(非推奨)
    'fe80::/10',      # リンクローカル全体
    '100::/64',
))


def is_public_address(value):
    """文字列のアドレスが公開アドレスなら True。判定は Python の is_global に揃え、IPv4 を埋め込む IPv6 は埋め込まれた側も見る。"""
    v = str(value).strip().strip('[]')
    if '%' in v:   # ゾーン付き(fe80::1%eth0・8.8.8.8%x 等)は拒否
        return False
    try:
        ip = ipaddress.ip_address(v)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        # NAT64(64:ff9b::/96)・6to4(2002::/16)は、埋め込まれた IPv4 が公開アドレスのときだけ通す(DNS64/NAT64 網で全ホストが拒否されないように)
        if ip in _NAT64:
            return is_public_address(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
        if ip in _6TO4:
            return is_public_address(str(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)))
        if any(ip in n for n in _EXTRA_BLOCKED_V6):
            return False
    if ip.is_multicast or ip.is_unspecified:   # is_global はマルチキャストを真と返すことがあるので明示的に除く
        return False
    return bool(ip.is_global)
