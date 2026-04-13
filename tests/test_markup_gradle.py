"""Tests for the Gradle build-script annotator."""

from token_savior.annotator import annotate


class TestGradleAnnotator:
    def test_build_gradle_kts_sections_are_extracted(self):
        text = """\
plugins {
    id("java-library")
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("org.junit:junit-bom:5.10.2")
    testImplementation("org.junit.jupiter:junit-jupiter")
}

tasks.test {
    useJUnitPlatform()
}
"""
        meta = annotate(text, source_name="build.gradle.kts")
        assert [section.title for section in meta.sections] == [
            "plugins",
            "id java-library",
            "repositories",
            "mavenCentral",
            "dependencies",
            "implementation org.junit:junit-bom:5.10.2",
            "testImplementation org.junit.jupiter:junit-jupiter",
            "tasks.test",
            "useJUnitPlatform",
        ]

    def test_settings_gradle_and_assignment_are_extracted(self):
        text = """\
rootProject.name = "demo"
include(":app", ":shared")
"""
        meta = annotate(text, source_name="settings.gradle.kts")
        assert [section.title for section in meta.sections] == [
            "rootProject.name",
            "include :app",
        ]
