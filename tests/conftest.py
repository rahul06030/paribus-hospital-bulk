import itertools
import json
import os

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

UPSTREAM = "https://upstream.test"

os.environ.update(
    HOSPITAL_API_BASE_URL=UPSTREAM,
    RETRY_BACKOFF_SECONDS="0",
    MAX_RETRIES="2",
)

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402


class FakeDirectory:
    """A stand-in for the Hospital Directory API with switchable failures."""

    def __init__(self):
        self.hospitals = {}
        self.ids = itertools.count(101)
        self.fail_names = set()  # names that always get a 500
        self.flaky_names = {}  # name -> number of 503s before success
        self.reject_names = set()  # names that get a 422
        self.save_then_fail_names = {}  # name -> number of creates that save but answer 500
        self.activate_status = 200
        self.delete_status = 200
        self.create_calls = 0

    def create(self, request: httpx.Request) -> httpx.Response:
        self.create_calls += 1
        body = json.loads(request.content)
        name = body["name"]
        if name in self.reject_names:
            return httpx.Response(422, json={"detail": "invalid hospital"})
        if name in self.fail_names:
            return httpx.Response(500, json={"detail": "boom"})
        if self.flaky_names.get(name, 0) > 0:
            self.flaky_names[name] -= 1
            return httpx.Response(503, json={"detail": "unavailable"})
        hospital = {**body, "id": next(self.ids), "active": False}
        self.hospitals[hospital["id"]] = hospital
        if self.save_then_fail_names.get(name, 0) > 0:
            self.save_then_fail_names[name] -= 1
            return httpx.Response(500, json={"detail": "saved, then crashed"})
        return httpx.Response(200, json=hospital)

    def batch(self, batch_id):
        return [h for h in self.hospitals.values() if h["creation_batch_id"] == batch_id]

    def activate(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        if self.activate_status != 200:
            return httpx.Response(self.activate_status, json={"detail": "activation failed"})
        hospitals = self.batch(batch_id)
        if not hospitals:
            return httpx.Response(404, json={"detail": "No hospitals found with the specified batch ID"})
        for h in hospitals:
            h["active"] = True
        return httpx.Response(200, json={"activated": len(hospitals)})

    def get_batch(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        hospitals = self.batch(batch_id)
        if not hospitals:
            return httpx.Response(404, json={"detail": "No hospitals found with the specified batch ID"})
        return httpx.Response(200, json=hospitals)

    def delete_batch(self, request: httpx.Request, batch_id: str) -> httpx.Response:
        if self.delete_status != 200:
            return httpx.Response(self.delete_status, json={"detail": "delete failed"})
        hospitals = self.batch(batch_id)
        for h in hospitals:
            del self.hospitals[h["id"]]
        return httpx.Response(200, json={"deleted": len(hospitals)})


@pytest.fixture
def directory():
    fake = FakeDirectory()
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as mock:
        mock.get("/", name="health").respond(200, json={"status": "ok"})
        mock.post("/hospitals/").mock(side_effect=fake.create)
        mock.patch(path__regex=r"/hospitals/batch/(?P<batch_id>[^/]+)/activate").mock(side_effect=fake.activate)
        mock.get(path__regex=r"/hospitals/batch/(?P<batch_id>[^/]+)$").mock(side_effect=fake.get_batch)
        mock.delete(path__regex=r"/hospitals/batch/(?P<batch_id>[^/]+)$").mock(side_effect=fake.delete_batch)
        fake.router = mock
        yield fake


@pytest.fixture
def client(directory):
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c


def make_csv(rows, header="name,address,phone"):
    lines = [header] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode()


def upload(client, content, path="/hospitals/bulk", **params):
    return client.post(path, files={"file": ("hospitals.csv", content, "text/csv")}, params=params)
