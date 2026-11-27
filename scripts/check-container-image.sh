#!/bin/sh
# Start the core image on an empty data folder with two private Docker networks
# (no internet, no LAN, no lamps) and check that Light Studio and the virtual
# bridge answer, that the first start wrote the data folder, and that the
# container stops cleanly. Usage: sh scripts/check-container-image.sh <image>
set -eu
image=$1
suffix=$$
data=$(mktemp -d)
# Only what this run created is removed, by ID.
container="" core="" lan=""
cleanup() {
    [ -n "$container" ] && docker rm -f "$container" >/dev/null 2>&1
    [ -n "$core" ] && docker network rm "$core" >/dev/null 2>&1
    [ -n "$lan" ] && docker network rm "$lan" >/dev/null 2>&1
    docker run --rm -v "$data:/data" --entrypoint sh "$image" -c 'rm -rf /data/* /data/.[!.]*' >/dev/null 2>&1
    rmdir "$data" 2>/dev/null
    return 0
}
trap cleanup EXIT
core=$(docker network create --internal --subnet 172.30.250.0/29 --gateway 172.30.250.1 "lcc-check-core-$suffix")
lan=$(docker network create --internal --subnet 10.250.0.0/24 --gateway 10.250.0.1 "lcc-check-lan-$suffix")
container=$(docker create --network "$core" --ip 172.30.250.3 \
    -e CORE_ADDRESS=10.250.0.40 -e TZ=Europe/Kyiv -e STUDIO_HOSTS=nas.local \
    -v "$data:/data" --stop-timeout 20 "$image")
docker network connect --ip 10.250.0.40 "$lan" "$container"
docker start "$container" >/dev/null

probe() {
    # Ask from a sibling container: the bridge as a LAN client would, Light Studio
    # on the core network as the NAS's published port does (Host names the NAS).
    network=$1
    shift
    docker run --rm --network "$network" --entrypoint python3 "$image" -I -c "
import sys, urllib.request
request = urllib.request.Request(sys.argv[1], headers={'Host': sys.argv[2]})
print(urllib.request.urlopen(request, timeout=3).read(4096).decode())" "$@"
}
ok=""
i=0
while [ $i -lt 60 ]; do
    if studio=$(probe "$core" http://172.30.250.3:19881/ nas.local:19881 2>/dev/null) &&
       bridge=$(probe "$lan" http://10.250.0.40/api/config 10.250.0.40 2>/dev/null); then
        ok=1
        break
    fi
    i=$((i + 1))
    sleep 2
done
if [ -z "$ok" ]; then
    docker logs "$container" 2>&1 | tail -60
    echo "The core did not answer" >&2
    exit 1
fi
printf '%s' "$studio" | grep -q "Light Studio"
printf '%s' "$bridge" | grep -q '"bridgeid"'
# Light Studio is not on the LAN address: only the NAS's published port reaches it.
# A TCP connect decides, whatever HTTP would answer: 0 open, 3 closed.
set +e
docker run --rm --network "$lan" --entrypoint python3 "$image" -I -c "
import socket, sys
try:
    socket.create_connection(('10.250.0.40', 19881), timeout=3).close()
except OSError:
    sys.exit(3)" >/dev/null 2>&1
lan_port=$?
set -e
if [ "$lan_port" != 3 ]; then
    echo "Light Studio's port on the LAN address: probe exit $lan_port (3 means closed)" >&2
    exit 1
fi
docker exec "$container" sh -c 'test -s /data/core.json && test -s /data/state/adapter-token && test -s /data/state/power-owner-token && test -s /data/version && test -d /data/state'
docker exec "$container" sh -c 'test "$(stat -c %a /data/state/adapter-token)" = 600'
start=$(date +%s)
docker stop "$container" >/dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' "$container")
elapsed=$(( $(date +%s) - start ))
if [ "$code" != 0 ] || [ "$elapsed" -ge 20 ]; then
    docker logs "$container" 2>&1 | tail -40
    echo "The core did not stop cleanly (exit $code after ${elapsed}s)" >&2
    exit 1
fi
# A second start keeps the identity.
before=$(docker run --rm -v "$data:/data" --entrypoint python3 "$image" -I -c "import json;print(json.load(open('/data/core.json'))['gateway']['mac'])")
docker start "$container" >/dev/null
sleep 5
docker stop "$container" >/dev/null
after=$(docker run --rm -v "$data:/data" --entrypoint python3 "$image" -I -c "import json;print(json.load(open('/data/core.json'))['gateway']['mac'])")
[ "$before" = "$after" ]
echo "Container image check passed"
