from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, engine, get_db
from models import AdminLog, PasswordResetToken, Tournament, User
from schemas import (
    AdminLogOut,
    AdminPasswordResetRequest,
    AdminStatsOut,
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
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


ADMIN_ROLES = {"admin", "super_admin"}
SUPER_ADMIN_ROLE = "super_admin"


def normalize_title(title: str) -> str:
    title = title.strip()
    return title or "未命名賽程"


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


@app.get("/health")
def health_check():
    return {"status": "ok", "database": "postgresql", "auth": "enabled"}


@app.get("/admin/users", response_model=list[AdminUserOut])
def admin_list_users(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.scalars(select(User).order_by(User.created_at.desc())).all()


@app.get("/admin/tournaments", response_model=list[AdminTournamentOut])
def admin_list_tournaments(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.scalars(select(Tournament).order_by(Tournament.updated_at.desc())).all()


@app.get("/admin/logs", response_model=list[AdminLogOut])
def admin_list_logs(
    admin_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(AdminLog).order_by(AdminLog.created_at.desc()).limit(200)
    ).all()


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

    write_admin_log(
        db=db,
        actor_user=admin_user,
        action="delete_tournament",
        target_type="tournament",
        target_id=tournament.id,
        details={
            "title": tournament.title,
            "owner_user_id": tournament.user_id,
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
    return tournaments


@app.post("/tournaments", response_model=TournamentOut, status_code=status.HTTP_201_CREATED)
def create_tournament(
    payload: TournamentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
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