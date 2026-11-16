"""The admin tab's API at ``/api/admin/``: users (``users.manage``) and roles (``roles.manage``).

Nobody can hand out more than they have. A user who may manage users or roles but isn't an admin
can only:
  * give a role whose permissions they all have themselves;
  * change, disable, reset or remove users whose permissions they all have;
  * put permissions they have into a role, and edit only roles within their own permissions.
So managing users never leads to becoming an admin. The locked Admin role, and the rule that the
last enabled admin stays, are enforced in ``Accounts``.
"""

from __future__ import annotations

from cbzwebp.web import current_user

from .accounts import PERMISSIONS, Accounts, clean_perms

DENIED = {"error": "you don't have permission for this"}
USER_ACTIONS = {"user-add", "user-role", "user-disable", "user-password", "user-remove"}
ROLE_ACTIONS = {"role-add", "role-update", "role-remove"}


class AdminAPI:
    def __init__(self, accounts: Accounts):
        self.accounts = accounts

    def state(self) -> dict:
        me = current_user()
        if not me:
            return {}
        perms, out = set(me["perms"]), {}
        if "users.manage" in perms:
            out["admin_users"] = [
                {k: u[k] for k in ("id", "name", "role_id", "role", "disabled", "admin")}
                for u in self.accounts.users()
            ]
        if perms & {"users.manage", "roles.manage"}:
            out["admin_roles"] = self.accounts.roles()
            out["permissions"] = [{"key": k, "label": label} for k, label in PERMISSIONS]
        return out

    def post(self, action: str, body: dict):
        me = current_user()
        if not me:
            return 401, {"error": "sign in first"}
        mine = set(me["perms"])
        if action in USER_ACTIONS and "users.manage" not in mine:
            return 403, DENIED
        if action in ROLE_ACTIONS and "roles.manage" not in mine:
            return 403, DENIED
        acc = self.accounts

        def within(perms) -> bool:
            return set(perms) <= mine

        def target():
            user = acc.get_id(int(body["id"]))
            if not within(user["perms"]):
                raise PermissionError("that user has permissions you don't have")
            return user

        def role_to_give():
            role = acc.role(int(body["role_id"]))
            if not within(role["perms"]):
                raise PermissionError(
                    f"you can't give the {role['name']} role: it has permissions you don't have"
                )
            return role

        try:
            if action == "user-add":
                role = role_to_give()
                return 200, acc.add(str(body.get("name", "")), str(body.get("password", "")), role=role["id"])
            if action == "user-role":
                user, role = target(), role_to_give()
                return 200, acc.set_role(user["name"], role["id"])
            if action == "user-disable":
                user = target()
                if user["id"] == me["id"]:
                    raise ValueError("you can't disable your own account")
                disabled = body.get("disabled", True)
                if not isinstance(disabled, bool):
                    raise ValueError("disabled must be true or false")
                acc.set_disabled(user["name"], disabled)
                return 200, acc.get_id(user["id"])
            if action == "user-password":
                user = target()
                acc.set_password(user["name"], str(body.get("password", "")))  # signs them out everywhere
                return 200, {"ok": True}
            if action == "user-remove":
                user = target()
                if user["id"] == me["id"]:
                    raise ValueError("you can't remove your own account")
                acc.remove(user["name"])
                return 200, {"ok": True}
            if action == "role-add":
                perms = clean_perms(body.get("perms") or [])  # unknown names: 400 first
                if not within(perms):
                    raise PermissionError("you can't give permissions you don't have")
                return 200, acc.add_role(str(body.get("name", "")), perms)
            if action == "role-update":
                role = acc.role(int(body["id"]))
                perms = body.get("perms")
                perms = None if perms is None else clean_perms(perms)
                if not role["locked"] and not within(role["perms"]):
                    raise PermissionError("that role has permissions you don't have")
                if perms is not None and not within(perms):
                    raise PermissionError("you can't give permissions you don't have")
                return 200, acc.update_role(role["id"], body.get("name"), perms)
            if action == "role-remove":
                role = acc.role(int(body["id"]))
                if not role["locked"] and not within(role["perms"]):
                    raise PermissionError("that role has permissions you don't have")
                acc.remove_role(role["id"])
                return 200, {"ok": True}
        except PermissionError as e:
            return 403, {"error": str(e)}
        except KeyError as e:
            return 404, {"error": str(e).strip("'\"")}
        return 404, {"error": "not found"}
