"""Tests for the Prisma schema annotator."""

from token_savior.prisma_annotator import annotate_prisma


SAMPLE_SCHEMA = """\
generator client {
  provider = "prisma-client-js"
}

datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
}

model User {
  id        Int      @id @default(autoincrement())
  email     String   @unique
  name      String?
  posts     Post[]
  createdAt DateTime @default(now())
}

model Post {
  id        Int      @id @default(autoincrement())
  title     String
  content   String?
  published Boolean  @default(false)
  author    User     @relation(fields: [authorId], references: [id])
  authorId  Int
}

enum Role {
  USER
  ADMIN
}
"""


def test_extracts_models():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    class_names = [c.name for c in meta.classes]
    assert "User" in class_names
    assert "Post" in class_names
    assert "Role" in class_names


def test_model_fields():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    user = next(c for c in meta.classes if c.name == "User")
    field_names = [m.name for m in user.methods]
    assert "id" in field_names
    assert "email" in field_names
    assert "name" in field_names
    assert "createdAt" in field_names


def test_field_qualified_names():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    user = next(c for c in meta.classes if c.name == "User")
    email_field = next(m for m in user.methods if m.name == "email")
    assert email_field.qualified_name == "User.email"
    assert email_field.parent_class == "User"


def test_base_classes_store_block_kind():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    user = next(c for c in meta.classes if c.name == "User")
    assert user.base_classes == ["model"]
    role = next(c for c in meta.classes if c.name == "Role")
    assert role.base_classes == ["enum"]


def test_enum_members():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    role = next(c for c in meta.classes if c.name == "Role")
    member_names = [m.name for m in role.methods]
    assert "USER" in member_names
    assert "ADMIN" in member_names


def test_generator_and_datasource_not_extracted():
    meta = annotate_prisma(SAMPLE_SCHEMA, "schema.prisma")
    class_names = [c.name for c in meta.classes]
    assert "client" not in class_names
    assert "db" not in class_names
