from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from mmgu.db import Base, Timestamped, utcnow


class CraftSkill(Base):
    """One character's level in one tradeskill, plus what they specialise in."""

    __tablename__ = "crafters_skills"
    __table_args__ = (UniqueConstraint("character_id", "skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("core_characters.id", ondelete="CASCADE"), index=True)
    skill: Mapped[str] = mapped_column(String(60), index=True)
    level: Mapped[int] = mapped_column(Integer, default=0)
    specialty: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Recipe(Base, Timestamped):
    """How to make something: the skill, what comes out, what goes in."""

    __tablename__ = "crafters_recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    skill: Mapped[str] = mapped_column(String(60), index=True)
    result_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    result_name: Mapped[str] = mapped_column(String(120), index=True)
    yield_qty: Mapped[int] = mapped_column(Integer, default=1)
    trivial: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("core_members.id"), nullable=True)


class RecipeComponent(Base):
    __tablename__ = "crafters_recipe_components"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("crafters_recipes.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("archive_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)
