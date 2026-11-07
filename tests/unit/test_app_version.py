from importlib import metadata

from video_sum_infra import app


def test_frozen_version_uses_distribution_metadata_over_nearby_checkout(monkeypatch, tmp_path):
    fake_module = tmp_path / "checkout" / "packages" / "infra" / "src" / "video_sum_infra" / "app.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.parents[4].joinpath("VERSION").write_text("9.9.9", encoding="utf-8")
    monkeypatch.setattr(app, "__file__", str(fake_module))
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app.metadata, "version", lambda name: "1.21.3")
    monkeypatch.setattr(app, "_is_running_from_installed_dist", lambda: False)
    assert app.AppInfo.load().version == "1.21.3"


def test_development_version_still_prefers_workspace(monkeypatch, tmp_path):
    fake_module = tmp_path / "checkout" / "packages" / "infra" / "src" / "video_sum_infra" / "app.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.parents[4].joinpath("VERSION").write_text("1.21.3", encoding="utf-8")
    monkeypatch.setattr(app, "__file__", str(fake_module))
    monkeypatch.delattr(app.sys, "frozen", raising=False)
    monkeypatch.setattr(app.metadata, "version", lambda name: "1.0.0")
    monkeypatch.setattr(app, "_is_running_from_installed_dist", lambda: False)
    assert app.AppInfo.load().version == "1.21.3"


def test_frozen_missing_metadata_cannot_be_masked_by_workspace(monkeypatch, tmp_path):
    fake_module = tmp_path / "checkout" / "packages" / "infra" / "src" / "video_sum_infra" / "app.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.parents[4].joinpath("VERSION").write_text("1.21.3", encoding="utf-8")
    monkeypatch.setattr(app, "__file__", str(fake_module))
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(app.metadata, "version", missing)
    assert app.AppInfo.load().version == "0.0.0"
