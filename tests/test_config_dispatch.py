"""Tests for config file dispatch through the main annotator."""

from token_savior.annotator import annotate


class TestConfigDispatch:
    def test_yaml_dispatch(self):
        meta = annotate("name: test", source_name="config.yaml")
        assert any(s.title == "name" for s in meta.sections)

    def test_yml_dispatch(self):
        meta = annotate("name: test", source_name="config.yml")
        assert any(s.title == "name" for s in meta.sections)

    def test_toml_dispatch(self):
        meta = annotate('name = "test"', source_name="config.toml")
        assert any(s.title == "name" for s in meta.sections)

    def test_ini_dispatch(self):
        meta = annotate("[section]\nkey = val", source_name="config.ini")
        assert any(s.title == "section" for s in meta.sections)

    def test_cfg_dispatch(self):
        meta = annotate("[section]\nkey = val", source_name="config.cfg")
        assert any(s.title == "section" for s in meta.sections)

    def test_properties_dispatch(self):
        meta = annotate("key=val", source_name="app.properties")
        assert any(s.title == "key" for s in meta.sections)

    def test_env_dispatch(self):
        meta = annotate("DB_HOST=localhost", source_name=".env")
        assert any(s.title == "DB_HOST" for s in meta.sections)

    def test_xml_dispatch(self):
        meta = annotate("<root><key>val</key></root>", source_name="config.xml")
        assert any(s.title == "root" for s in meta.sections)

    def test_plist_dispatch(self):
        meta = annotate("<plist><dict/></plist>", source_name="info.plist")
        assert any("plist" in s.title for s in meta.sections)

    def test_hcl_dispatch(self):
        meta = annotate('variable "x" {\n  default = "y"\n}', source_name="main.tf")
        assert len(meta.sections) > 0

    def test_conf_dispatch(self):
        meta = annotate("key = value", source_name="app.conf")
        assert any(s.title == "key" for s in meta.sections)
