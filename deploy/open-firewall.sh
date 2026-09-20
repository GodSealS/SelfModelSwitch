#!/bin/sh
# Open the compatibility port to ONE explicit network, and only to that network.
#
# The scheduler's control plane is a Unix socket with peer credentials; it never
# listens on TCP, so nothing here may widen it. Only the compatibility port
# (`server.port`) is ever opened, and the network is an explicit deployment
# input: opening 0.0.0.0/0 needs `--allow-any` on purpose.
#
# The rule is idempotent: it is added only when `iptables -C` says it is absent,
# so a restart never stacks duplicates.
#
#   open-firewall.sh --cidr 192.168.55.0/24 --port 8090            # add (idempotent)
#   open-firewall.sh --cidr 192.168.55.0/24 --port 8090 --remove   # remove
#   open-firewall.sh --cidr 192.168.55.0/24 --port 8090 --check    # 0 present, 1 absent
#
# Exit codes: 0 ok, 1 absent (only with --check), 2 input refused, 3 iptables unusable.
set -eu

CIDR=""
PORT=""
REMOVE=0
CHECK=0
ALLOW_ANY=0
IPTABLES="${IPTABLES:-iptables}"

usage() {
    echo "usage: open-firewall.sh --cidr <CIDR> --port <port> [--remove] [--check] [--allow-any]" >&2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --cidr) CIDR="${2-}"; shift 2 ;;
        --port) PORT="${2-}"; shift 2 ;;
        --remove) REMOVE=1; shift ;;
        --check) CHECK=1; shift ;;
        --allow-any) ALLOW_ANY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "open-firewall.sh: unknown argument '$1'" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$CIDR" ] || [ -z "$PORT" ]; then
    echo "open-firewall.sh: --cidr and --port are required" >&2
    usage
    exit 2
fi

case "$PORT" in
    *[!0-9]*)
        echo "open-firewall.sh: '$PORT' is not a port number" >&2; exit 2 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "open-firewall.sh: '$PORT' is out of range" >&2; exit 2
fi

if ! command -v "$IPTABLES" >/dev/null 2>&1; then
    echo "open-firewall.sh: '$IPTABLES' is not available" >&2
    exit 3
fi

# `--cidr` takes one network or a comma-separated list, so a deployment that is
# reached over two internal links names both without opening anything else.
NETWORKS=$(echo "$CIDR" | tr ',' ' ')
for NETWORK in $NETWORKS; do
    case "$NETWORK" in
        *[!0-9./]*)
            echo "open-firewall.sh: '$NETWORK' is not a CIDR" >&2; exit 2 ;;
    esac
    if ! echo "$NETWORK" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$'; then
        echo "open-firewall.sh: '$NETWORK' is not a CIDR" >&2; exit 2
    fi
    if [ "$NETWORK" = "0.0.0.0/0" ] && [ "$ALLOW_ANY" -eq 0 ]; then
        echo "open-firewall.sh: 0.0.0.0/0 needs --allow-any: the whole internet is not a deployment input" >&2
        exit 2
    fi
done

ABSENT=0
for NETWORK in $NETWORKS; do
    RULE="-p tcp -s $NETWORK --dport $PORT -j ACCEPT"
    if "$IPTABLES" -C INPUT $RULE >/dev/null 2>&1; then
        PRESENT=1
    else
        PRESENT=0
        ABSENT=$((ABSENT + 1))
    fi
    if [ "$CHECK" -eq 1 ]; then
        [ "$PRESENT" -eq 1 ] && echo "present: $NETWORK -> $PORT" || echo "absent: $NETWORK -> $PORT"
        continue
    fi
    if [ "$REMOVE" -eq 1 ]; then
        if [ "$PRESENT" -eq 1 ]; then
            "$IPTABLES" -D INPUT $RULE
            echo "removed $NETWORK -> $PORT"
        else
            echo "absent, nothing to remove: $NETWORK -> $PORT"
        fi
        continue
    fi
    if [ "$PRESENT" -eq 1 ]; then
        echo "already present: $NETWORK -> $PORT"
    else
        "$IPTABLES" -A INPUT $RULE
        echo "added $NETWORK -> $PORT"
    fi
done

if [ "$CHECK" -eq 1 ]; then
    if [ "$ABSENT" -eq 0 ]; then exit 0; else exit 1; fi
fi
