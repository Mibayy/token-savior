"""Tests for Spring Boot route/env support and Java entry point scoring."""

from __future__ import annotations

import textwrap

from token_savior.project_indexer import ProjectIndexer
from token_savior.query_api import create_project_query_functions


def _write(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _build_spring_project(root) -> None:
    _write(
        root / "src/main/java/com/acme/app/Application.java",
        """\
        package com.acme.app;

        import org.springframework.boot.autoconfigure.SpringBootApplication;

        @SpringBootApplication
        public class Application {
            public static void main(String[] args) {
            }
        }
        """,
    )
    _write(
        root / "src/main/java/com/acme/web/UserController.java",
        """\
        package com.acme.web;

        import org.springframework.beans.factory.annotation.Value;
        import org.springframework.web.bind.annotation.GetMapping;
        import org.springframework.web.bind.annotation.PathVariable;
        import org.springframework.web.bind.annotation.PostMapping;
        import org.springframework.web.bind.annotation.RequestBody;
        import org.springframework.web.bind.annotation.RequestMapping;
        import org.springframework.web.bind.annotation.RequestMethod;
        import org.springframework.web.bind.annotation.RestController;

        @RestController
        @RequestMapping("/api/users")
        public class UserController {
            @Value("${APP_MODE}")
            private String mode;

            @GetMapping("/{id}")
            public String getUser(@PathVariable String id) {
                return System.getenv("APP_MODE");
            }

            @PostMapping
            public String createUser(@RequestBody String body) {
                return body;
            }

            @RequestMapping(value = "/{id}", method = RequestMethod.DELETE)
            public void deleteUser(@PathVariable String id) {
            }
        }
        """,
    )


class TestSpringBootQuerySupport:
    def test_detects_spring_boot_routes_env_usage_and_entry_points(self, tmp_path):
        root = tmp_path / "spring-project"
        root.mkdir()
        _build_spring_project(root)

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        routes = funcs["get_routes"]()
        assert any(
            route["route"] == "/api/users/{id}"
            and route["methods"] == ["GET"]
            and route["type"] == "api"
            for route in routes
        )
        assert any(
            route["route"] == "/api/users" and route["methods"] == ["POST"] for route in routes
        )
        assert any(
            route["route"] == "/api/users/{id}" and route["methods"] == ["DELETE"]
            for route in routes
        )
        assert len(
            {
                (route["route"], tuple(route["methods"]), route["file"], route.get("line"))
                for route in routes
            }
        ) == len(routes)

        env_usage = funcs["get_env_usage"]("APP_MODE")
        assert any(entry["usage_type"] == "system.getenv" for entry in env_usage)
        assert any(entry["usage_type"] == "spring.value" for entry in env_usage)

        entry_points = funcs["get_entry_points"]()
        assert any(
            entry["name"] == "com.acme.app.Application.main(String[])"
            or entry["name"].endswith(".Application.main(String[])")
            for entry in entry_points
        )
        assert any(
            entry["name"].endswith(".UserController.getUser(String)")
            and entry["score"] > 0.5
            and "spring route mapping" in entry["reasons"]
            for entry in entry_points
        )

    def test_adjacent_spring_methods_do_not_inherit_next_route_annotation(self, tmp_path):
        root = tmp_path / "spring-project"
        root.mkdir()
        _write(
            root / "src/main/java/com/acme/web/ConfigController.java",
            """\
            package com.acme.web;

            import org.springframework.web.bind.annotation.GetMapping;
            import org.springframework.web.bind.annotation.RequestMapping;
            import org.springframework.web.bind.annotation.RestController;

            @RestController
            @RequestMapping("/api/config")
            public class ConfigController {
                @GetMapping("/effective")
                public String effective() {
                    return "effective";
                }

                @GetMapping("/history")
                public String history() {
                    return "history";
                }

                @GetMapping("/access")
                public String access() {
                    return "access";
                }
            }
            """,
        )

        idx = ProjectIndexer(str(root)).index()
        funcs = create_project_query_functions(idx)

        routes = [
            (route["route"], tuple(route["methods"]), route["line"])
            for route in funcs["get_routes"]()
        ]

        assert routes == [
            ("/api/config/access", ("GET",), 21),
            ("/api/config/effective", ("GET",), 11),
            ("/api/config/history", ("GET",), 16),
        ]
