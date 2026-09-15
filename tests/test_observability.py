from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import User
from app.security import create_session_token, hash_password
from app.services.tenants import get_or_create_reference_tenant


def _admin_client(tmp_path, monkeypatch, langsmith_api_key=None):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        chroma_db_path=str(tmp_path / "chroma"),
        secret_key="test-secret",
        mesh_api_key=None,
        langsmith_api_key=langsmith_api_key,
    )
    app = create_app(settings)
    with app.state.session_factory() as session:
        tenant = get_or_create_reference_tenant(session)
        session.add(
            User(
                tenant_id=tenant.id,
                email="curator@test.dev",
                password_hash=hash_password("password123"),
                role="admin",
            )
        )
        session.commit()
    test_client = TestClient(app)
    test_client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    return test_client


def _make_non_admin(client: TestClient, email: str) -> None:
    """No self-registration path remains for non-admin accounts (the AI-engineer
    login/register surface was removed) — create one directly and mint its session
    cookie the way login used to, same pattern as tests/test_mvp_api.py."""
    with client.app.state.session_factory() as session:
        user = User(
            tenant_id=1,
            email=email,
            password_hash=hash_password("password123"),
            role="user",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        token = create_session_token(session, user, client.app.state.settings)
    client.cookies.set(client.app.state.settings.session_cookie_name, token)


def test_observability_requires_admin(client: TestClient) -> None:
    _make_non_admin(client, "user@test.dev")
    response = client.get("/api/admin/observability/runs")
    assert response.status_code == 403


def test_observability_unavailable_without_langsmith_key(tmp_path, monkeypatch) -> None:
    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key=None)
    response = admin_client.get("/api/admin/observability/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["runs"] == []
    assert "LANGSMITH_API_KEY" in body["message"]


def test_observability_returns_recent_runs(tmp_path, monkeypatch) -> None:
    class FakeRun:
        def __init__(self, id_, name, status, error=None, tags=None):
            self.id = id_
            self.trace_id = id_
            self.name = name
            self.run_type = "chain"
            self.status = status
            self.error = error
            self.start_time = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
            self.end_time = datetime(2026, 8, 8, 12, 0, 2, tzinfo=timezone.utc)
            self.session_id = "fake-session"
            self.tags = tags or []

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def list_runs(self, **kwargs):
            # fetch_recent_runs makes two calls: the root-run list (execution_order=1)
            # and a bulk pipeline_latency_ms lookup over each page's trace_ids (no
            # execution_order kwarg) — this test only cares about the former.
            if kwargs.get("execution_order") != 1:
                return iter([])
            return iter(
                [
                    FakeRun("1", "agent_pipeline", "success", tags=["visitor:v7"]),
                    FakeRun("2", "agent_pipeline", "error", error="Mesh timeout"),
                ]
            )

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert len(body["runs"]) == 2
    assert body["runs"][0]["name"] == "agent_pipeline"
    assert body["runs"][0]["latency_ms"] == 2000
    assert body["runs"][0]["visitor_id"] == "v7"
    assert body["runs"][0]["pipeline_latency_ms"] is None
    assert body["runs"][1]["error"] == "Mesh timeout"
    assert body["runs"][1]["visitor_id"] is None
    assert body["runs"][0]["url"] == "https://smith.langchain.com/fake/1"


def test_observability_runs_computes_pipeline_latency_via_one_bulk_query(
    tmp_path, monkeypatch
) -> None:
    """pipeline_latency_ms must be the sum of a trace's own top-level named steps
    (analyze/retrieve/rerank/grade_refine/generate/store), fetched via one extra
    bulk list_runs call for the whole page — not a per-row read_run(load_child_runs)
    call, and never double-counting a nested span like mesh_generate_narrative
    (contained inside generate_narrative's own latency_ms already)."""

    class FakeRun:
        def __init__(
            self, id_, name, trace_id=None, start_time=None, end_time=None, tags=None
        ):
            self.id = id_
            self.trace_id = trace_id or id_
            self.name = name
            self.run_type = "chain"
            self.status = "success"
            self.error = None
            self.start_time = start_time
            self.end_time = end_time
            self.session_id = "fake-session"
            self.tags = tags or []

    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    roots = [
        FakeRun(
            "root-a",
            "agent_pipeline",
            start_time=t0,
            end_time=t0 + timedelta(seconds=10),
        ),
        FakeRun(
            "root-b",
            "agent_pipeline",
            start_time=t0 + timedelta(minutes=1),
            end_time=t0 + timedelta(minutes=1, seconds=5),
        ),
    ]
    # root-a's own children: 1000ms + 2000ms = 3000ms real pipeline work, plus a
    # nested mesh_generate_narrative (2000ms, same window as generate_narrative) that
    # must NOT add to the sum — deliberately included here (as if a server-side filter
    # quirk let it through) to prove the client-side name re-check actually excludes
    # it, not just that the fake happens to omit it. root-b gets no children at all
    # (no named step ran).
    children = [
        FakeRun(
            "step-1",
            "analyze_activity",
            trace_id="root-a",
            start_time=t0,
            end_time=t0 + timedelta(seconds=1),
        ),
        FakeRun(
            "step-2",
            "generate_narrative",
            trace_id="root-a",
            start_time=t0 + timedelta(seconds=1),
            end_time=t0 + timedelta(seconds=3),
        ),
        FakeRun(
            "step-3",
            "mesh_generate_narrative",
            trace_id="root-a",
            start_time=t0 + timedelta(seconds=1),
            end_time=t0 + timedelta(seconds=3),
        ),
    ]

    call_count = {"n": 0}

    class FakeClient:
        def __init__(self, api_key=None):
            pass

        def list_runs(self, **kwargs):
            call_count["n"] += 1
            if kwargs.get("execution_order") == 1:
                return iter(roots)
            # The one bulk children query for the whole page.
            return iter(children)

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs")
    body = response.json()
    runs_by_id = {run["id"]: run for run in body["runs"]}

    assert runs_by_id["root-a"]["pipeline_latency_ms"] == 3000.0
    assert runs_by_id["root-b"]["pipeline_latency_ms"] is None
    # Exactly 2 list_runs calls total for the whole page — not one per row.
    assert call_count["n"] == 2


def test_observability_paginates_runs(tmp_path, monkeypatch) -> None:
    class FakeRun:
        def __init__(self, id_, name, status, start_time):
            self.id = id_
            self.trace_id = id_
            self.name = name
            self.run_type = "chain"
            self.status = status
            self.error = None
            self.start_time = start_time
            self.end_time = start_time + timedelta(seconds=2)
            self.session_id = "fake-session"
            self.tags = []

    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def list_runs(self, **kwargs):
            if kwargs.get("execution_order") != 1:
                return iter([])
            # Deliberately yielded oldest-first (run "1" has the earliest start_time)
            # — fetch_recent_runs must sort newest-first itself before paging, so the
            # page order can't just be trusting this iterator's own order.
            return iter(
                [
                    FakeRun("1", "agent_pipeline", "success", t0),
                    FakeRun(
                        "2", "agent_pipeline", "success", t0 + timedelta(minutes=1)
                    ),
                    FakeRun(
                        "3", "agent_pipeline", "success", t0 + timedelta(minutes=2)
                    ),
                ]
            )

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")

    first_page = admin_client.get("/api/admin/observability/runs?limit=2&offset=0")
    body = first_page.json()
    assert [run["id"] for run in body["runs"]] == ["3", "2"]
    assert body["has_more"] is True

    second_page = admin_client.get("/api/admin/observability/runs?limit=2&offset=2")
    body = second_page.json()
    assert [run["id"] for run in body["runs"]] == ["1"]
    assert body["has_more"] is False


def test_observability_runs_scopes_to_one_visitor_via_native_tag_filter(
    tmp_path, monkeypatch
) -> None:
    """Each agent_pipeline run is tagged visitor:<id> at trace time
    (prepare_retrieval_recommendation) — a visitor_id query param must turn into a
    real server-side LangSmith filter (has(tags, "visitor:<id>")), not a client-side
    filter over the unscoped run list, so this only asserts on what list_runs was
    actually called with."""

    class FakeRun:
        def __init__(self, id_):
            self.id = id_
            self.trace_id = id_
            self.name = "agent_pipeline"
            self.run_type = "chain"
            self.status = "success"
            self.error = None
            self.start_time = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
            self.end_time = self.start_time + timedelta(seconds=1)
            self.session_id = "fake-session"
            self.tags = ["visitor:v42"]

    captured_calls = []

    class FakeClient:
        def __init__(self, api_key=None):
            pass

        def list_runs(self, **kwargs):
            captured_calls.append(kwargs)
            if kwargs.get("execution_order") != 1:
                return iter(
                    []
                )  # the bulk pipeline_latency_ms lookup — not under test here
            return iter([FakeRun("only-this-visitors-run")])

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")

    response = admin_client.get("/api/admin/observability/runs?visitor_id=v42")
    assert response.status_code == 200
    root_call = next(
        call for call in captured_calls if call.get("execution_order") == 1
    )
    assert root_call.get("filter") == 'has(tags, "visitor:v42")'
    body = response.json()
    assert [run["id"] for run in body["runs"]] == ["only-this-visitors-run"]
    assert body["runs"][0]["visitor_id"] == "v42"

    # Without visitor_id, no filter is sent at all — the unscoped "all visitors" view.
    captured_calls.clear()
    admin_client.get("/api/admin/observability/runs")
    root_call = next(
        call for call in captured_calls if call.get("execution_order") == 1
    )
    assert "filter" not in root_call


def test_observability_reports_api_failure(tmp_path, monkeypatch) -> None:
    class FailingClient:
        def __init__(self, api_key=None):
            pass

        def list_runs(self, **kwargs):
            raise RuntimeError("connection refused")

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FailingClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "connection refused" in body["message"]


def test_observability_run_detail_requires_admin(client: TestClient) -> None:
    _make_non_admin(client, "detailuser@test.dev")
    response = client.get("/api/admin/observability/runs/some-id")
    assert response.status_code == 403


def test_observability_run_detail_unavailable_without_langsmith_key(
    tmp_path, monkeypatch
) -> None:
    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key=None)
    response = admin_client.get("/api/admin/observability/runs/some-id")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["run"] is None


class _FakeRun:
    def __init__(
        self,
        id_,
        name,
        run_type="chain",
        status="success",
        error=None,
        start_time=None,
        end_time=None,
        inputs=None,
        outputs=None,
        child_runs=None,
    ):
        self.id = id_
        self.name = name
        self.run_type = run_type
        self.status = status
        self.error = error
        self.start_time = start_time
        self.end_time = end_time
        self.inputs = inputs or {}
        self.outputs = outputs or {}
        self.child_runs = child_runs or []
        self.session_id = "fake-session"


def test_observability_run_detail_filters_noise_and_orders_steps(
    tmp_path, monkeypatch
) -> None:
    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

    def at(seconds):
        return t0 + timedelta(seconds=seconds)

    # analyze_activity started before retrieve_models, but is listed second in
    # child_runs — the response must still order steps by start_time, not list order.
    retrieve = _FakeRun(
        "retrieve-id",
        "retrieve_models",
        run_type="retriever",
        start_time=at(2),
        end_time=at(3),
        inputs={"state": {"a": 1}},
        outputs={"candidates_scored": []},
    )
    analyze = _FakeRun(
        "analyze-id",
        "analyze_activity",
        start_time=at(0),
        end_time=at(1),
        inputs={"state": {"user_id": 5}},
        outputs={"behavior_summary": "x"},
    )
    # A LangGraph-internal "noise" node wrapping the two named ones — must be skipped,
    # but its children must still surface, at depth 0 (not nested under the noise).
    langgraph_noise = _FakeRun("noise-id", "LangGraph", child_runs=[retrieve, analyze])
    root = _FakeRun(
        "root-id",
        "agent_pipeline",
        start_time=at(0),
        end_time=at(5),
        child_runs=[langgraph_noise],
    )

    class FakeClient:
        def __init__(self, api_key=None):
            pass

        def read_run(self, run_id, load_child_runs=False):
            assert run_id == "root-id"
            assert load_child_runs is True
            return root

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs/root-id")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    run = body["run"]
    assert run["id"] == "root-id"
    assert run["name"] == "agent_pipeline"
    assert [step["name"] for step in run["steps"]] == [
        "analyze_activity",
        "retrieve_models",
    ]
    assert all(step["depth"] == 0 for step in run["steps"])
    assert run["steps"][0]["outputs"] == {"behavior_summary": "x"}
    assert run["url"] == "https://smith.langchain.com/fake/root-id"
    # analyze (1000ms) + retrieve (1000ms) = 2000ms of real pipeline work, vs the
    # root's own 5000ms wall time (at(0) to at(5)) — the gap is exactly the kind of
    # LangGraph-internal overhead (the skipped "LangGraph" noise wrapper here) this
    # field exists to separate out.
    assert run["pipeline_latency_ms"] == 2000.0
    assert run["latency_ms"] == 5000.0


def test_observability_run_detail_sorts_across_separate_top_level_branches(
    tmp_path, monkeypatch
) -> None:
    """Regression test: real traces can have multiple separate top-level branches
    (e.g. each grade_refine retry under its own internal wrapper span rather than a
    single shared parent) — sorting only within each branch doesn't produce a globally
    chronological list. The response must sort the fully flattened step list."""
    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

    def at(seconds):
        return t0 + timedelta(seconds=seconds)

    # Second branch (listed first in child_runs) starts *later* than the first branch
    # (listed second) — a per-branch-only sort would emit retrieve_models before
    # analyze_activity.
    branch_two = _FakeRun(
        "branch-two",
        "grade_refine",
        start_time=at(10),
        end_time=at(11),
    )
    branch_one = _FakeRun(
        "branch-one",
        "analyze_activity",
        start_time=at(0),
        end_time=at(1),
    )
    root = _FakeRun(
        "root-id",
        "agent_pipeline",
        start_time=at(0),
        end_time=at(11),
        child_runs=[branch_two, branch_one],
    )

    class FakeClient:
        def __init__(self, api_key=None):
            pass

        def read_run(self, run_id, load_child_runs=False):
            return root

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs/root-id")
    body = response.json()
    assert [step["name"] for step in body["run"]["steps"]] == [
        "analyze_activity",
        "grade_refine",
    ]


def test_observability_run_detail_pipeline_latency_is_none_with_no_named_steps(
    tmp_path, monkeypatch
) -> None:
    """A run with no recognized named steps (e.g. it short-circuited before any of our
    own nodes ran) must report pipeline_latency_ms as None, not 0 — 0ms would falsely
    read as "the pipeline ran instantly" rather than "we have no data"."""
    t0 = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
    root = _FakeRun(
        "root-id", "agent_pipeline", start_time=t0, end_time=t0 + timedelta(seconds=1)
    )

    class FakeClient:
        def __init__(self, api_key=None):
            pass

        def read_run(self, run_id, load_child_runs=False):
            return root

        def get_run_url(self, *, run):
            return f"https://smith.langchain.com/fake/{run.id}"

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FakeClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs/root-id")
    body = response.json()
    assert body["run"]["steps"] == []
    assert body["run"]["pipeline_latency_ms"] is None
    assert body["run"]["latency_ms"] == 1000.0


def test_observability_run_detail_reports_api_failure(tmp_path, monkeypatch) -> None:
    class FailingClient:
        def __init__(self, api_key=None):
            pass

        def read_run(self, run_id, load_child_runs=False):
            raise RuntimeError("not found")

    import app.services.observability as observability_module

    monkeypatch.setattr(observability_module, "Client", FailingClient)

    admin_client = _admin_client(tmp_path, monkeypatch, langsmith_api_key="fake-key")
    response = admin_client.get("/api/admin/observability/runs/missing-id")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "not found" in body["message"]
