"""管理 CLI（v1 沒有管理頁，先以指令維護組織、成員、團隊與邀請；設計文件 §4.3）。

    docker exec <容器> python -m app.cli create-org "公司名稱"
    docker exec <容器> python -m app.cli create-member --org "公司名稱" --name 王小明 --employee-code E001 --admin --invite
    docker exec <容器> python -m app.cli add-to-team --member E001 --team 業務部
    docker exec <容器> python -m app.cli invite --member E001
    docker exec <容器> python -m app.cli revoke-binding --member E001      # 換手機／換 LINE 後重新邀請
    docker exec <容器> python -m app.cli set-status --member E001 --status suspended
    docker exec <容器> python -m app.cli list-members

成員可用 UUID 或員工編號指定。邀請 token 只顯示這一次，資料庫只存雜湊。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import create_invitation, revoke_access, revoke_binding
from app.db.engine import create_engine, session_factory
from app.db.models import ExternalIdentity, Organization, OrganizationRole, Team, TeamMember, User


class CliError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _org(s: AsyncSession, ref: str) -> Organization:
    by_id = _uuid(ref)
    orgs = (await s.scalars(select(Organization).where(
        Organization.id == by_id if by_id else Organization.name == ref))).all()
    if len(orgs) != 1:
        raise CliError(f"找不到組織「{ref}」" if not orgs else f"有多個組織叫「{ref}」，請改用 ID")
    return orgs[0]


async def _member(s: AsyncSession, ref: str) -> User:
    by_id = _uuid(ref)
    users = (await s.scalars(select(User).where(
        User.id == by_id if by_id else User.employee_code == ref))).all()
    if len(users) != 1:
        raise CliError(f"找不到成員「{ref}」" if not users else f"員工編號「{ref}」對應多位成員，請改用 ID")
    return users[0]


def _invite_link(token: str) -> str:
    base = os.environ.get("LIFF_URL", "")
    return f"{base.rstrip('/')}/bind?invite={quote(token)}" if base else f"邀請碼：{token}"


async def _print_invite(s: AsyncSession, member: User, hours: float) -> None:
    token = await create_invitation(s, member, now=_now(), ttl=timedelta(hours=hours))
    print(f"已建立邀請（{hours:g} 小時內有效、只能使用一次）：")
    print(_invite_link(token))


async def run(args: argparse.Namespace) -> None:
    engine = create_engine()
    try:
        async with session_factory(engine)() as s:
            await COMMANDS[args.command](s, args)
            await s.commit()
    finally:
        await engine.dispose()


async def cmd_create_org(s: AsyncSession, a) -> None:
    org = Organization(name=a.name, timezone=a.timezone)
    s.add(org)
    await s.flush()
    print(f"已建立組織 {org.name}（{org.id}）")


async def cmd_create_member(s: AsyncSession, a) -> None:
    org = await _org(s, a.org)
    member = User(organization_id=org.id, display_name=a.name, employee_code=a.employee_code,
                  timezone=org.timezone)
    s.add(member)
    await s.flush()
    if a.admin:
        s.add(OrganizationRole(organization_id=org.id, user_id=member.id, role="admin"))
    print(f"已建立成員 {member.display_name}（{member.id}）" + ("，並設為管理員" if a.admin else ""))
    if a.invite:
        await _print_invite(s, member, a.hours)


async def cmd_invite(s: AsyncSession, a) -> None:
    member = await _member(s, a.member)
    if member.status != "active":
        raise CliError("成員不是啟用狀態，無法邀請")
    if await s.scalar(select(ExternalIdentity).where(ExternalIdentity.user_id == member.id)):
        raise CliError("成員已綁定 LINE；若要換帳號，請先執行 revoke-binding")
    await _print_invite(s, member, a.hours)


async def cmd_revoke_binding(s: AsyncSession, a) -> None:
    member = await _member(s, a.member)
    had = await revoke_binding(s, member, now=_now())
    print(f"已解除 {member.display_name} 的 LINE 綁定，並撤銷所有登入" if had
          else f"{member.display_name} 原本沒有綁定；已撤銷所有登入")
    if a.invite:
        await _print_invite(s, member, a.hours)


async def cmd_set_status(s: AsyncSession, a) -> None:
    member = await _member(s, a.member)
    member.status = a.status
    if a.status != "active":
        await revoke_access(s, member, now=_now())  # 停權／離職即撤銷 session；保留綁定以便復職（§4.3）
    print(f"{member.display_name} 狀態已改為 {a.status}")


async def cmd_add_to_team(s: AsyncSession, a) -> None:
    member = await _member(s, a.member)
    team = await s.scalar(select(Team).where(Team.organization_id == member.organization_id, Team.name == a.team))
    if team is None:
        team = Team(organization_id=member.organization_id, name=a.team)
        s.add(team)
        await s.flush()
        print(f"已建立團隊 {team.name}")
    exists = await s.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == member.id))
    if exists is None:
        s.add(TeamMember(organization_id=member.organization_id, team_id=team.id, user_id=member.id))
    print(f"{member.display_name} 已在團隊 {team.name}")


async def cmd_remove_from_team(s: AsyncSession, a) -> None:
    member = await _member(s, a.member)
    row = await s.scalar(select(TeamMember).join(Team, Team.id == TeamMember.team_id).where(
        TeamMember.user_id == member.id, Team.name == a.team, Team.organization_id == member.organization_id))
    if row is None:
        raise CliError(f"{member.display_name} 不在團隊 {a.team}")
    await s.delete(row)
    print(f"已將 {member.display_name} 移出團隊 {a.team}")


async def cmd_list_members(s: AsyncSession, a) -> None:
    stmt = select(User, Organization.name).join(Organization, Organization.id == User.organization_id)
    if a.org:
        org = await _org(s, a.org)
        stmt = stmt.where(User.organization_id == org.id)
    for user, org_name in (await s.execute(stmt.order_by(Organization.name, User.display_name))).all():
        bound = await s.scalar(select(ExternalIdentity.id).where(ExternalIdentity.user_id == user.id))
        admin = await s.scalar(select(OrganizationRole.id).where(OrganizationRole.user_id == user.id))
        teams = (await s.scalars(select(Team.name).join(TeamMember, TeamMember.team_id == Team.id)
                                 .where(TeamMember.user_id == user.id))).all()
        print("\t".join([str(user.id), org_name, user.display_name, user.employee_code or "-", user.status,
                         "已綁定" if bound else "未綁定", "管理員" if admin else "", ",".join(teams)]))


COMMANDS = {
    "create-org": cmd_create_org,
    "create-member": cmd_create_member,
    "invite": cmd_invite,
    "revoke-binding": cmd_revoke_binding,
    "set-status": cmd_set_status,
    "add-to-team": cmd_add_to_team,
    "remove-from-team": cmd_remove_from_team,
    "list-members": cmd_list_members,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="LINE 團隊行程助理管理指令")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create-org", help="建立組織")
    c.add_argument("name")
    c.add_argument("--timezone", default="Asia/Taipei")

    c = sub.add_parser("create-member", help="建立成員")
    c.add_argument("--org", required=True, help="組織名稱或 ID")
    c.add_argument("--name", required=True)
    c.add_argument("--employee-code")
    c.add_argument("--admin", action="store_true")
    c.add_argument("--invite", action="store_true", help="同時產生綁定邀請")
    c.add_argument("--hours", type=float, default=72)

    c = sub.add_parser("invite", help="產生綁定邀請（會撤銷該成員舊的未使用邀請）")
    c.add_argument("--member", required=True, help="成員 ID 或員工編號")
    c.add_argument("--hours", type=float, default=72)

    c = sub.add_parser("revoke-binding", help="解除 LINE 綁定並撤銷登入")
    c.add_argument("--member", required=True)
    c.add_argument("--invite", action="store_true", help="同時產生新邀請")
    c.add_argument("--hours", type=float, default=72)

    c = sub.add_parser("set-status", help="啟用／停權／離職")
    c.add_argument("--member", required=True)
    c.add_argument("--status", required=True, choices=["active", "suspended", "left"])

    for name in ("add-to-team", "remove-from-team"):
        c = sub.add_parser(name, help="加入團隊（不存在則建立）" if name == "add-to-team" else "移出團隊")
        c.add_argument("--member", required=True)
        c.add_argument("--team", required=True)

    c = sub.add_parser("list-members", help="列出成員")
    c.add_argument("--org")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except CliError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
