from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, engine, get_db
from models import AdminLog, PasswordResetToken, Tournament, User
from schemas import (
    AdminLogOut,
    AdminPasswordResetRequest,
    AdminStatsOut,
    AdminTournamentDetailOut,
    AdminTournamentOut,
    AdminUserOut,
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    ResetPasswordRequest,
    TokenResponse,
    TournamentCreate,
    TournamentListItem,
    TournamentOut,
    TournamentUpdate,
    UserCreate,
    UserLogin,
    UserOut,
)
from security import (
    create_access_token,
    create_reset_token,
    hash_password,
    hash_reset_token,
    verify_password,
)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Tournament Manager Auth PostgreSQL API",
    description="Auth-enabled tournament manager backend with PostgreSQL persistence.",
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"


ADMIN_ROLES = {"admin", "super_admin"}
SUPER_ADMIN_ROLE = "super_admin"


def normalize_title(title: str) -> str:
    title = title.strip()
    return title or "未命名賽程"


def is_power_of_two(value: int) -> bool:
    return value > 0 and (value & (value - 1)) == 0


def natural_sort_key(text: str) -> list:
    """Match frontend Intl.Collator numeric sorting for names like Team 9 vs Team 10."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(text))]


def get_tournament_format(state: dict | None) -> str:
    if not isinstance(state, dict):
        return "single_elimination"
    return state.get("format") or "single_elimination"


def build_group_rank_map(group_stage: dict) -> dict[str, list[str]]:
    groups = group_stage.get("groups") or []
    match_results = group_stage.get("results") or []
    rank_map: dict[str, list[str]] = {}

    for group in groups:
        group_name = str(group.get("name") or "").strip()
        teams = [str(team).strip() for team in (group.get("teams") or []) if str(team).strip()]
        if not group_name or not teams:
            continue
        wins = {team: 0 for team in teams}

        for result in match_results:
            if str(result.get("group") or "").strip() != group_name:
                continue
            winner = str(result.get("winner") or "").strip()
            loser = str(result.get("loser") or "").strip()
            if winner in wins and loser in wins and winner != loser:
                wins[winner] += 1

        ranked = sorted(teams, key=lambda team: (-wins[team], natural_sort_key(team)))
        rank_map[group_name] = ranked
    return rank_map


def extract_opening_round_assignments(state: dict) -> list[str]:
    tournament_data = state.get("tournamentData") or {}
    if not isinstance(tournament_data, dict):
        return []

    assigned: list[str] = []
    for match_id in sorted(tournament_data.keys()):
        if not str(match_id).startswith("W_R1"):
            continue
        match_state = tournament_data[match_id]
        if not isinstance(match_state, dict):
            continue
        for slot in ("slot1", "slot2"):
            value = str(match_state.get(slot) or "").strip()
            if value and value != "__BYE__":
                assigned.append(value)
    return assigned


def extract_knockout_team_names(state: dict) -> set[str]:
    names: set[str] = set()
    tournament_data = state.get("tournamentData") or {}
    if isinstance(tournament_data, dict):
        for match_state in tournament_data.values():
            if not isinstance(match_state, dict):
                continue
            for slot in ("slot1", "slot2"):
                value = str(match_state.get(slot) or "").strip()
                if value and value != "__BYE__":
                    names.add(value)
    team_names = state.get("teamNames") or []
    if isinstance(team_names, list):
        for team in team_names:
            value = str(team).strip()
            if value:
                names.add(value)
    return names


def validate_group_knockout_state(state: dict, mode: str) -> None:
    group_stage = state.get("group_stage") or {}
    knockout_stage = state.get("knockout_stage") or {}
    groups = group_stage.get("groups") or []
    entrants = [str(team).strip() for team in (knockout_stage.get("entrants") or []) if str(team).strip()]

    if not groups:
        raise HTTPException(status_code=422, detail="group_stage.groups is required for group_knockout mode")
    if len(groups) < 1 or len(groups) > 42:
        raise HTTPException(status_code=422, detail="Group count must be between 1 and 42")

    total_teams = int(group_stage.get("total_teams") or 0)
    if total_teams and (total_teams < 4 or total_teams > 128):
        raise HTTPException(status_code=422, detail="group_stage.total_teams must be between 4 and 128")

    group_team_set: set[str] = set()
    advance_rules = group_stage.get("advance_rules") or []
    rank_map = build_group_rank_map(group_stage)
    qualified_set: set[str] = set()
    grouped_team_total = 0

    for group in groups:
        group_name = str(group.get("name") or "").strip()
        group_teams = [str(team).strip() for team in (group.get("teams") or []) if str(team).strip()]
        if not group_name:
            raise HTTPException(status_code=422, detail="Each group must have a non-empty name")
        if len(group_teams) < 2:
            raise HTTPException(status_code=422, detail=f"Group {group_name} must contain at least 2 teams")
        grouped_team_total += len(group_teams)
        if len(set(group_teams)) != len(group_teams):
            raise HTTPException(status_code=422, detail=f"Group {group_name} contains duplicated team names")
        for team in group_teams:
            if team in group_team_set:
                raise HTTPException(status_code=422, detail=f"Team {team} appears in multiple groups")
            group_team_set.add(team)

    if grouped_team_total < 4 or grouped_team_total > 128:
        raise HTTPException(status_code=422, detail="Total group teams must be between 4 and 128")
    if total_teams and grouped_team_total != total_teams:
        raise HTTPException(status_code=422, detail="Sum of group team sizes must equal group_stage.total_teams")

    if mode == "group_knockout":
        for rule in advance_rules:
            group_name = str(rule.get("group") or "").strip()
            top_n = int(rule.get("top_n") or 0)
            ranked = rank_map.get(group_name) or []
            if not group_name:
                raise HTTPException(status_code=422, detail="advance_rules.group is required")
            if top_n <= 0:
                raise HTTPException(status_code=422, detail=f"advance_rules for {group_name} must have top_n >= 1")
            if top_n > len(ranked):
                raise HTTPException(status_code=422, detail=f"advance_rules for {group_name} exceeds group team count")
            qualified_set.update(ranked[:top_n])

    if mode != "group_knockout" or not entrants:
        return

    for team in entrants:
        if team not in group_team_set:
            raise HTTPException(
                status_code=422,
                detail=f"Knockout entrant {team} is not in group stage team list",
            )
        if team not in qualified_set:
            raise HTTPException(
                status_code=422,
                detail=f"Knockout entrant {team} is not qualified from group stage",
            )

    if len(set(entrants)) != len(entrants):
        raise HTTPException(status_code=422, detail="Knockout entrants cannot contain duplicates")

    opening_assigned = extract_opening_round_assignments(state)
    if opening_assigned:
        if len(opening_assigned) != len(set(opening_assigned)):
            raise HTTPException(
                status_code=422,
                detail="Opening round cannot contain duplicate team names",
            )
        if set(opening_assigned) != set(entrants) or len(opening_assigned) != len(entrants):
            raise HTTPException(
                status_code=422,
                detail="Opening round teams must match knockout entrants exactly",
            )

    knockout_teams = extract_knockout_team_names(state)
    for team in knockout_teams:
        if team not in group_team_set:
            raise HTTPException(
                status_code=422,
                detail=f"Knockout team {team} is not in group stage team list",
            )
        if team not in qualified_set:
            raise HTTPException(
                status_code=422,
                detail=f"Knockout team {team} is not qualified from group stage",
            )

    max_losses = int(knockout_stage.get("max_losses") or 1)
    if max_losses == 2 and not is_power_of_two(len(entrants)):
        raise HTTPException(
            status_code=422,
            detail="Double elimination in knockout stage requires entrant count to be a power of two",
        )


def validate_tournament_state(payload: TournamentCreate | TournamentUpdate) -> None:
    data = payload.model_dump(exclude_unset=True)
    state = data.get("state")
    if not isinstance(state, dict):
        return

    mode = state.get("format") or "single_elimination"
    if mode not in {"single_elimination", "group_knockout", "group_only"}:
        raise HTTPException(status_code=422, detail="Unsupported tournament format")

    if mode in {"group_knockout", "group_only"}:
        validate_group_knockout_state(state, mode)


def update_tournament_from_payload(tournament: Tournament, payload: TournamentCreate | TournamentUpdate) -> None:
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        tournament.title = normalize_title(data["title"])
    if "state" in data and data["state"] is not None:
        tournament.state = data["state"]
    if "champion" in data:
        tournament.champion = data["champion"]
        tournament.completed_at = datetime.now(timezone.utc) if data["champion"] else None
    if "team_count" in data:
        tournament.team_count = data["team_count"]
    if "max_losses" in data:
        tournament.max_losses = data["max_losses"]
    if "has_grand_final_reset" in data:
        tournament.has_grand_final_reset = bool(data["has_grand_final_reset"])


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user


def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != SUPER_ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Super admin only")
    return current_user


def write_admin_log(
    db: Session,
    actor_user: User,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    details: dict | None = None,
) -> None:
    log = AdminLog(
        actor_user_id=actor_user.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )
    db.add(log)


def get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def ensure_user_management_permission(
    actor_user: User,
    target_user: User,
    *,
    forbid_self: bool = False,
) -> None:
    # Admin can only operate member accounts.
    if actor_user.role == "admin":
        if target_user.role != "member":
            raise HTTPException(status_code=403, detail="Admin can only manage member users")
        return

    # Super admin can operate member/admin but not super_admin.
    if actor_user.role == SUPER_ADMIN_ROLE:
        if forbid_self and actor_user.id == target_user.id:
            raise HTTPException(status_code=400, detail="Cannot manage your own account for this action")
        if target_user.role == SUPER_ADMIN_ROLE:
            raise HTTPException(status_code=403, detail="Cannot manage super admin users")
        return

    raise HTTPException(status_code=403, detail="Admin only")


def build_admin_tournament_out(tournament: Tournament, owner: User | None) -> AdminTournamentOut:
    state = tournament.state if isinstance(tournament.state, dict) else {}
    return AdminTournamentOut(
        id=tournament.id,
        user_id=tournament.user_id,
        owner_username=owner.username if owner else None,
        owner_role=owner.role if owner else None,
        title=tournament.title,
        champion=tournament.champion,
        team_count=tournament.team_count,
        max_losses=tournament.max_losses,
        has_grand_final_reset=tournament.has_grand_final_reset,
        format=get_tournament_format(state),
        completed_at=tournament.completed_at,
        created_at=tournament.created_at,
        updated_at=tournament.updated_at,
    )


def build_tournament_list_item(tournament: Tournament) -> TournamentListItem:
    state = tournament.state if isinstance(tournament.state, dict) else {}
    return TournamentListItem(
        id=tournament.id,
        title=tournament.title,
        champion=tournament.champion,
        team_count=tournament.team_count,
        max_losses=tournament.max_losses,
        has_grand_final_reset=tournament.has_grand_final_reset,
        format=get_tournament_format(state),
        completed_at=tournament.completed_at,
        created_at=tournament.created_at,
        updated_at=tournament.updated_at,
    )


def build_admin_tournament_detail(tournament: Tournament, owner: User | None) -> AdminTournamentDetailOut:
    base = build_admin_tournament_out(tournament, owner)
    return AdminTournamentDetailOut(**base.model_dump(), state=tournament.state or {})


def build_username_map(db: Session, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    users = db.scalars(select(User).where(User.id.in_(user_ids))).all()
    return {user.id: user.username for user in users}


@app.get("/health")
def health_check():
    return {"status": "ok", "database": "postgresql", "auth": "enabled"}


@app.get("/admin/users", response_model=list[AdminUserOut])
def admin_list_users(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.scalars(select(User).order_by(User.id.asc())).all()


@app.get("/admin/tournaments", response_model=list[AdminTournamentOut])
def admin_list_tournaments(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tournaments = db.scalars(select(Tournament).order_by(Tournament.id.asc())).all()
    owners = {
        user.id: user
        for user in db.scalars(
            select(User).where(User.id.in_({item.user_id for item in tournaments}))
        ).all()
    } if tournaments else {}
    return [build_admin_tournament_out(item, owners.get(item.user_id)) for item in tournaments]


@app.get("/admin/tournaments/{tournament_id}", response_model=AdminTournamentDetailOut)
def admin_get_tournament(
    tournament_id: int,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tournament = db.get(Tournament, tournament_id)
    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")
    owner = db.get(User, tournament.user_id)
    return build_admin_tournament_detail(tournament, owner)


@app.get("/admin/logs", response_model=list[AdminLogOut])
def admin_list_logs(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    logs = db.scalars(select(AdminLog).order_by(AdminLog.id.asc()).limit(200)).all()
    related_ids: set[int] = set()
    for log in logs:
        if log.actor_user_id is not None:
            related_ids.add(log.actor_user_id)
        if log.target_type == "user" and log.target_id is not None:
            related_ids.add(log.target_id)
    username_map = build_username_map(db, related_ids)
    return [
        AdminLogOut(
            id=log.id,
            actor_user_id=log.actor_user_id,
            actor_username=username_map.get(log.actor_user_id) if log.actor_user_id else None,
            action=log.action,
            target_type=log.target_type,
            target_id=log.target_id,
            target_username=(
                username_map.get(log.target_id)
                if log.target_type == "user" and log.target_id is not None
                else None
            ),
            details=log.details or {},
            created_at=log.created_at,
        )
        for log in logs
    ]


@app.get("/admin/stats", response_model=AdminStatsOut)
def admin_stats(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total_users = db.scalar(select(func.count(User.id))) or 0
    total_members = db.scalar(select(func.count(User.id)).where(User.role == "member")) or 0
    total_admins = db.scalar(select(func.count(User.id)).where(User.role == "admin")) or 0
    total_super_admins = db.scalar(select(func.count(User.id)).where(User.role == "super_admin")) or 0
    total_tournaments = db.scalar(select(func.count(Tournament.id))) or 0

    return AdminStatsOut(
        total_users=total_users,
        total_members=total_members,
        total_admins=total_admins,
        total_super_admins=total_super_admins,
        total_tournaments=total_tournaments,
    )


@app.put("/admin/users/{user_id}/promote", response_model=AdminUserOut)
def promote_user_to_admin(
    user_id: int,
    super_admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "super_admin":
        raise HTTPException(status_code=400, detail="Cannot promote a super admin")
    if user.role == "admin":
        return user

    old_role = user.role
    user.role = "admin"

    write_admin_log(
        db=db,
        actor_user=super_admin,
        action="promote_to_admin",
        target_type="user",
        target_id=user.id,
        details={
            "old_role": old_role,
            "new_role": "admin",
            "email": user.email,
        },
    )

    db.commit()
    db.refresh(user)
    return user


@app.put("/admin/users/{user_id}/demote", response_model=AdminUserOut)
def demote_admin_to_member(
    user_id: int,
    super_admin: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "super_admin":
        raise HTTPException(status_code=400, detail="Cannot demote a super admin")
    if user.role == "member":
        return user

    old_role = user.role
    user.role = "member"

    write_admin_log(
        db=db,
        actor_user=super_admin,
        action="demote_to_member",
        target_type="user",
        target_id=user.id,
        details={
            "old_role": old_role,
            "new_role": "member",
            "email": user.email,
        },
    )

    db.commit()
    db.refresh(user)
    return user


@app.put("/admin/users/{user_id}/disable", response_model=AdminUserOut)
def admin_disable_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = get_user_or_404(db, user_id)
    ensure_user_management_permission(admin_user, user, forbid_self=True)

    old_is_active = user.is_active
    user.is_active = False

    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="disable_user",
        target_type="user",
        target_id=user.id,
        details={
            "email": user.email,
            "old_is_active": old_is_active,
            "new_is_active": user.is_active,
        },
    )
    db.commit()
    db.refresh(user)
    return user


@app.put("/admin/users/{user_id}/enable", response_model=AdminUserOut)
def admin_enable_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = get_user_or_404(db, user_id)
    ensure_user_management_permission(admin_user, user, forbid_self=True)

    old_is_active = user.is_active
    user.is_active = True

    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="enable_user",
        target_type="user",
        target_id=user.id,
        details={
            "email": user.email,
            "old_is_active": old_is_active,
            "new_is_active": user.is_active,
        },
    )
    db.commit()
    db.refresh(user)
    return user


@app.delete("/admin/users/{user_id}")
def admin_delete_user(
    user_id: int,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = get_user_or_404(db, user_id)
    ensure_user_management_permission(admin_user, user, forbid_self=True)

    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="delete_user",
        target_type="user",
        target_id=user.id,
        details={
            "email": user.email,
            "username": user.username,
            "role": user.role,
        },
    )
    db.delete(user)
    db.commit()
    return {"deleted": True}


@app.put("/admin/users/{user_id}/reset-password", response_model=AdminUserOut)
def admin_reset_user_password(
    user_id: int,
    payload: AdminPasswordResetRequest,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = get_user_or_404(db, user_id)
    ensure_user_management_permission(admin_user, user)

    user.password_hash = hash_password(payload.new_password)
    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="reset_user_password",
        target_type="user",
        target_id=user.id,
        details={
            "email": user.email,
        },
    )
    db.commit()
    db.refresh(user)
    return user


@app.delete("/admin/tournaments/{tournament_id}")
def admin_delete_tournament(
    tournament_id: int,
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tournament = db.get(Tournament, tournament_id)
    if tournament is None:
        raise HTTPException(status_code=404, detail="Tournament not found")

    owner = db.get(User, tournament.user_id)
    # Regular admins can only delete tournaments owned by members.
    if admin_user.role == "admin":
        owner_role = owner.role if owner else "member"
        if owner_role in ADMIN_ROLES:
            raise HTTPException(
                status_code=403,
                detail="Admin cannot delete tournaments created by admin or super_admin",
            )

    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="delete_tournament",
        target_type="tournament",
        target_id=tournament.id,
        details={
            "title": tournament.title,
            "owner_user_id": tournament.user_id,
            "owner_username": owner.username if owner else None,
            "champion": tournament.champion,
        },
    )
    db.delete(tournament)
    db.commit()
    return {"deleted": True}


@app.post("/auth/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    existing = db.scalar(
        select(User).where(or_(User.email == payload.email, User.username == payload.username))
    )
    if existing:
        if existing.email == payload.email:
            raise HTTPException(status_code=409, detail="Email already registered")
        raise HTTPException(status_code=409, detail="Username already taken")

    user = User(
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="member",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=user)


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is inactive")
    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user=user)


@app.get("/auth/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@app.post("/auth/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        return ForgotPasswordResponse(
            ok=True,
            message="If this email exists, a reset token has been created.",
            reset_token=None,
        )

    raw_token = create_reset_token()
    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_reset_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(reset_token)
    db.commit()

    return ForgotPasswordResponse(
        ok=True,
        message="Development mode: use this reset token to set a new password.",
        reset_token=raw_token,
    )


@app.post("/auth/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    token_hash = hash_reset_token(payload.token)
    reset_token = db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    now = datetime.now(timezone.utc)
    if reset_token is None or reset_token.used_at is not None or reset_token.expires_at < now:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.get(User, reset_token.user_id)
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid reset token")

    user.password_hash = hash_password(payload.new_password)
    reset_token.used_at = now
    db.commit()
    return {"ok": True}


@app.put("/account/password")
def change_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@app.delete("/account")
def delete_account(
    payload: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role == "super_admin":
        raise HTTPException(status_code=400, detail="Super admin account cannot be deleted here")
    if not verify_password(payload.password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")
    db.delete(current_user)
    db.commit()
    return {"deleted": True}


@app.get("/tournaments", response_model=list[TournamentListItem])
def list_tournaments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournaments = db.scalars(
        select(Tournament)
        .where(Tournament.user_id == current_user.id)
        .order_by(Tournament.updated_at.desc())
    ).all()
    return [build_tournament_list_item(item) for item in tournaments]


@app.post("/tournaments", response_model=TournamentOut, status_code=status.HTTP_201_CREATED)
def create_tournament(
    payload: TournamentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    validate_tournament_state(payload)
    tournament = Tournament(
        user_id=current_user.id,
        title=normalize_title(payload.title),
        state=payload.state,
        champion=payload.champion,
        team_count=payload.team_count,
        max_losses=payload.max_losses,
        has_grand_final_reset=payload.has_grand_final_reset,
        completed_at=datetime.now(timezone.utc) if payload.champion else None,
    )
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    return tournament


@app.get("/tournaments/{tournament_id}", response_model=TournamentOut)
def get_tournament(
    tournament_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = db.get(Tournament, tournament_id)
    if tournament is None or tournament.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tournament not found")
    return tournament


@app.put("/tournaments/{tournament_id}", response_model=TournamentOut)
def update_tournament(
    tournament_id: int,
    payload: TournamentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    validate_tournament_state(payload)
    tournament = db.get(Tournament, tournament_id)
    if tournament is None or tournament.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tournament not found")
    update_tournament_from_payload(tournament, payload)
    db.commit()
    db.refresh(tournament)
    return tournament


@app.delete("/tournaments/{tournament_id}")
def delete_tournament(
    tournament_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tournament = db.get(Tournament, tournament_id)
    if tournament is None or tournament.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Tournament not found")
    db.delete(tournament)
    db.commit()
    return {"deleted": True}


@app.get("/", include_in_schema=False)
def serve_frontend():
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(INDEX_HTML)