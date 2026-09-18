"""Shared eligibility and settings reads for the separate AI preference layer."""

from sqlalchemy import or_
from sqlmodel import Session, select

from .models import KnowledgeTree, Node, PreferenceLearningState, PreferenceSupplement
from .schemas import PreferenceLearningOut, PreferenceSupplementOut

INITIAL_LEARNING_REVISION = "initial"


def read_learning_state(session: Session) -> PreferenceLearningOut:
    """Reading default settings never creates a row or changes a concurrency token."""
    state = session.get(PreferenceLearningState, 1, populate_existing=True)
    return PreferenceLearningOut(
        enabled=state.enabled if state else True,
        revision=state.revision if state else INITIAL_LEARNING_REVISION,
    )


def supplement_source_valid(session: Session, supplement: PreferenceSupplement) -> bool:
    if supplement.scope == "topic":
        if supplement.tree_id is None or session.get(KnowledgeTree, supplement.tree_id) is None:
            return False
    elif supplement.scope != "global" or supplement.tree_id is not None:
        return False
    if supplement.source_node_id is None or supplement.source_tree_id is None:
        return False
    source = session.get(Node, supplement.source_node_id, populate_existing=True)
    return bool(
        source
        and source.status == "complete"
        and source.tree_id == supplement.source_tree_id
        and session.get(KnowledgeTree, source.tree_id) is not None
        and (supplement.scope != "topic" or supplement.tree_id == source.tree_id)
    )


def eligible_supplements(session: Session, tree_id: int | None) -> list[PreferenceSupplement]:
    """Only applicable, active records with surviving completed sources may affect answers."""
    scopes = [PreferenceSupplement.scope == "global"]
    if tree_id is not None:
        scopes.append(
            (PreferenceSupplement.scope == "topic") & (PreferenceSupplement.tree_id == tree_id)
        )
    rows = session.exec(
        select(PreferenceSupplement)
        .where(PreferenceSupplement.status == "active", or_(*scopes))
        .order_by(PreferenceSupplement.updated_at.desc(), PreferenceSupplement.id.desc())
        .execution_options(populate_existing=True)
    ).all()
    return [row for row in rows if supplement_source_valid(session, row)]


def supplement_out(session: Session, supplement: PreferenceSupplement) -> PreferenceSupplementOut:
    tree = session.get(KnowledgeTree, supplement.tree_id) if supplement.tree_id else None
    source_tree = (
        session.get(KnowledgeTree, supplement.source_tree_id) if supplement.source_tree_id else None
    )
    return PreferenceSupplementOut(
        **supplement.model_dump(),
        tree_title=tree.title if tree else None,
        source_tree_title=source_tree.title if source_tree else None,
    )
