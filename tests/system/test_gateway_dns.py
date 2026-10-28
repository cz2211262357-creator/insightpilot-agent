"""Actual Nginx DNS re-resolution using only isolated synthetic containers."""

import ipaddress
import os
import time
from pathlib import Path
from uuid import uuid4

import docker
import httpx
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool

PYTHON = "python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
NGINX = (
    "mirror.gcr.io/library/nginx:1.28-alpine@sha256:"
    "a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"
)
SYNTHETIC_API = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, os
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        body=json.dumps({'backend':os.environ['SYNTHETIC_BACKEND'],'path':self.path}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(('0.0.0.0',8010),Handler).serve_forever()
"""
REQUEST_PATH = "/api/v1/check?alpha=a%2Fb&value=17"


def create_owned_network(client, identity, labels):
    existing = []
    for net in client.networks.list():
        for item in net.attrs.get("IPAM", {}).get("Config") or []:
            try:
                existing.append(ipaddress.ip_network(item["Subnet"], strict=False))
            except (KeyError, ValueError):
                continue
    start = int(identity[:2], 16)
    for offset in range(256):
        subnet = ipaddress.ip_network(f"10.241.{(start + offset) % 256}.0/24")
        if any(subnet.version == other.version and subnet.overlaps(other) for other in existing):
            continue
        try:
            return client.networks.create(
                "ip-gateway-test-" + identity,
                driver="bridge",
                labels=labels,
                ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))]),
            )
        except APIError as error:
            if "overlap" not in str(error).lower() and "pool" not in str(error).lower():
                raise
    raise AssertionError("No isolated test subnet available")


def ensure_image(client, image):
    try:
        client.images.get(image)
    except ImageNotFound:
        client.images.pull(image)


def await_api(container):
    deadline = time.monotonic() + 15
    command = [
        "python3",
        "-c",
        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8010/api/ready',timeout=1).read()",
    ]
    while time.monotonic() < deadline:
        if container.exec_run(command).exit_code == 0:
            return
        time.sleep(0.15)
    raise AssertionError("Synthetic API did not start")


def test_nginx_routes_to_recreated_api_and_preserves_path_and_query():
    identity = uuid4().hex
    labels = {
        "io.insightpilot.fixture": "gateway-" + identity,
        "io.insightpilot.fixture-parent": str(os.getpid()),
    }
    client = docker.from_env()
    network = None
    owned = []
    try:
        for image in (PYTHON, NGINX):
            ensure_image(client, image)
        network = create_owned_network(client, identity, labels)

        def container(name, image, command=None, *, aliases=None, ipv4=None, **kwargs):
            endpoint = client.api.create_endpoint_config(aliases=aliases, ipv4_address=ipv4)
            resource = client.containers.create(
                image=image,
                name="ip-gateway-test-" + identity + "-" + name,
                command=command,
                detach=True,
                labels=labels,
                network=network.name,
                networking_config={network.name: endpoint},
                **kwargs,
            )
            owned.append(resource)
            resource.start()
            resource.reload()
            assert (
                resource.labels.get("io.insightpilot.fixture") == labels["io.insightpilot.fixture"]
            )
            return resource

        api1 = container(
            "api1",
            PYTHON,
            ["python3", "-u", "-c", SYNTHETIC_API],
            aliases=["api"],
            environment={"SYNTHETIC_BACKEND": "api1"},
        )
        await_api(api1)
        api1.reload()
        first_ip = api1.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
        config = Path(__file__).resolve().parents[2] / "frontend/nginx.conf"
        web = container(
            "web",
            NGINX,
            ports={"8080/tcp": ("127.0.0.1", None)},
            volumes={str(config): {"bind": "/etc/nginx/conf.d/default.conf", "mode": "ro"}},
        )
        web.reload()
        bindings = web.attrs["NetworkSettings"]["Ports"]["8080/tcp"]
        assert len(bindings) == 1 and bindings[0]["HostIp"] == "127.0.0.1"
        url = f"http://127.0.0.1:{bindings[0]['HostPort']}" + REQUEST_PATH

        with httpx.Client(trust_env=False, timeout=2) as http:
            first = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    response = http.get(url)
                    if response.status_code == 200:
                        first = response.json()
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.15)
            assert first == {"backend": "api1", "path": REQUEST_PATH}

            api1.remove(force=True)
            placeholder = container(
                "reserved",
                PYTHON,
                ["python3", "-u", "-c", "import time; time.sleep(90)"],
                ipv4=first_ip,
            )
            assert (
                placeholder.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
                == first_ip
            )
            api2 = container(
                "api2",
                PYTHON,
                ["python3", "-u", "-c", SYNTHETIC_API],
                aliases=["api"],
                environment={"SYNTHETIC_BACKEND": "api2"},
            )
            await_api(api2)
            api2.reload()
            second_ip = api2.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]
            assert second_ip != first_ip, "Replacement API must have a different IP"
            last_status, result = None, None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    response = http.get(url)
                    last_status = response.status_code
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("backend") == "api2":
                            break
                except httpx.TransportError:
                    last_status = "transport_error"
                time.sleep(0.25)
            assert result == {"backend": "api2", "path": REQUEST_PATH}, (
                f"Nginx failed to route to the new API IP; last HTTP status={last_status}"
            )
    finally:
        for item in reversed(owned):
            try:
                item.reload()
            except NotFound:
                continue
            assert item.labels.get("io.insightpilot.fixture") == labels["io.insightpilot.fixture"]
            item.remove(force=True)
        if network is not None:
            network.reload()
            assert (
                network.attrs["Labels"]["io.insightpilot.fixture"]
                == labels["io.insightpilot.fixture"]
            )
            network.remove()
        client.close()
