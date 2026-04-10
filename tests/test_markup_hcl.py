"""Tests for the HCL/Terraform annotator."""

from token_savior.hcl_annotator import annotate_hcl


class TestHclResourceBlock:
    """Tests for resource block detection."""

    def test_resource_block_title(self):
        text = """resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "resource aws_instance web" in titles

    def test_resource_block_level(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-0c55b159cbfafe1f0"
}
"""
        meta = annotate_hcl(text)
        resource_section = next(s for s in meta.sections if s.title == "resource aws_instance web")
        assert resource_section.level == 1

    def test_resource_block_line_range(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-0c55b159cbfafe1f0"
}
"""
        meta = annotate_hcl(text)
        resource_section = next(s for s in meta.sections if s.title == "resource aws_instance web")
        assert resource_section.line_range.start == 1


class TestHclVariableBlock:
    """Tests for variable block with a single label."""

    def test_variable_block_title(self):
        text = """variable "instance_type" {
  default = "t2.micro"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "variable instance_type" in titles

    def test_variable_block_level(self):
        text = """variable "region" {
  default = "us-east-1"
}
"""
        meta = annotate_hcl(text)
        var_section = next(s for s in meta.sections if s.title == "variable region")
        assert var_section.level == 1


class TestHclSourceNameDefault:
    """Tests for default source_name."""

    def test_source_name_default(self):
        meta = annotate_hcl("")
        assert meta.source_name == "<hcl>"

    def test_source_name_custom(self):
        meta = annotate_hcl("", source_name="main.tf")
        assert meta.source_name == "main.tf"


class TestHclKeyValuePairs:
    """Tests for key-value pair detection inside blocks."""

    def test_kv_inside_resource(self):
        text = """resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t2.micro"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "ami" in titles
        assert "instance_type" in titles

    def test_kv_level_inside_block(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-0c55b159cbfafe1f0"
}
"""
        meta = annotate_hcl(text)
        ami_section = next(s for s in meta.sections if s.title == "ami")
        # Inside a depth-1 block, kv should be level 2
        assert ami_section.level == 2

    def test_kv_line_number(self):
        text = """resource "aws_s3_bucket" "main" {
  bucket = "my-bucket"
  acl    = "private"
}
"""
        meta = annotate_hcl(text)
        bucket_section = next(s for s in meta.sections if s.title == "bucket")
        assert bucket_section.line_range.start == 2


class TestHclNestedBlocks:
    """Tests for nested block detection (provisioner inside resource)."""

    def test_nested_block_detected(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-abc123"

  provisioner "local-exec" {
    command = "echo Hello"
  }
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "provisioner local-exec" in titles

    def test_nested_block_level(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-abc123"

  provisioner "local-exec" {
    command = "echo Hello"
  }
}
"""
        meta = annotate_hcl(text)
        prov_section = next(s for s in meta.sections if s.title == "provisioner local-exec")
        # Inside a depth-1 block, nested block is level 2
        assert prov_section.level == 2

    def test_nested_kv_level(self):
        text = """resource "aws_instance" "web" {
  provisioner "local-exec" {
    command = "echo Hello"
  }
}
"""
        meta = annotate_hcl(text)
        cmd_section = next(s for s in meta.sections if s.title == "command")
        # Inside depth-2 block, kv is level 3
        assert cmd_section.level == 3


class TestHclComments:
    """Tests that comments are properly skipped."""

    def test_hash_comment_skipped(self):
        text = """# This is a comment
resource "aws_instance" "web" {
  # inline comment
  ami = "ami-abc"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        # Comment lines should not appear as sections
        assert not any("comment" in t.lower() for t in titles)
        assert "resource aws_instance web" in titles

    def test_double_slash_comment_skipped(self):
        text = """// This is a comment
variable "name" {
  default = "test"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert not any(t.startswith("//") for t in titles)
        assert "variable name" in titles


class TestHclDepthCap:
    """Tests that depth is capped at 4."""

    def test_depth_capped_at_4(self):
        # 5 levels of nesting
        text = """resource "a" "b" {
  block1 "c" {
    block2 "d" {
      block3 "e" {
        block4 "f" {
          key = "value"
        }
      }
    }
  }
}
"""
        meta = annotate_hcl(text)
        max_level = max(s.level for s in meta.sections)
        assert max_level <= 4


class TestHclFallbackToGeneric:
    """Test that empty/no-section HCL falls back to annotate_generic."""

    def test_empty_text_fallback(self):
        meta = annotate_hcl("")
        # Generic fallback: no sections
        assert meta.sections == []
        assert meta.total_chars == 0


class TestHclMultipleBlocks:
    """Tests for multiple top-level blocks."""

    def test_multiple_resource_blocks(self):
        text = """resource "aws_instance" "web" {
  ami = "ami-web"
}

resource "aws_s3_bucket" "storage" {
  bucket = "my-storage"
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "resource aws_instance web" in titles
        assert "resource aws_s3_bucket storage" in titles

    def test_output_block(self):
        text = """output "instance_ip" {
  value = aws_instance.web.public_ip
}
"""
        meta = annotate_hcl(text)
        titles = [s.title for s in meta.sections]
        assert "output instance_ip" in titles


class TestHclAnnotatorDispatch:
    """Test that the annotator dispatch routes .tf/.hcl correctly."""

    def test_tf_dispatch(self):
        from token_savior.annotator import annotate

        text = """resource "aws_instance" "web" {
  ami = "ami-abc"
}
"""
        meta = annotate(text, source_name="main.tf")
        titles = [s.title for s in meta.sections]
        assert "resource aws_instance web" in titles

    def test_hcl_dispatch(self):
        from token_savior.annotator import annotate

        text = """variable "region" {
  default = "us-east-1"
}
"""
        meta = annotate(text, source_name="variables.hcl")
        titles = [s.title for s in meta.sections]
        assert "variable region" in titles
