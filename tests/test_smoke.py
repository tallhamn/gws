from gws.config import Settings
from gws.api import create_app


def test_create_app_has_health_route():
    app = create_app(Settings(database_url="sqlite+pysqlite:///:memory:"))
    paths = {route.path for route in app.routes}
    assert "/healthz" in paths
