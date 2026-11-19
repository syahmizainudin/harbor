"""The admin tab's API at ``/api/admin/``: users (``users.manage``) and roles (``roles.manage``).

Nobody can hand out more than they have. A user who may manage users or roles but isn't an admin
can only:
  * give a role whose permissions they all have themselves;
  * change, disable, reset or remove users whose permissions they all have;
  * put permissions they have into a role, and edit only roles within their own permissions.
So managing users never leads to becoming an admin. The locked Admin role, and the rule that the
last enabled admin stays, are enforced in ``Accounts``.

Library access (``roles.manage``): who may see each library. A non-admin changes it only for
libraries they can see, and only for roles within their own permissions.
"""

from __future__ import annotations

from cbzwebp.web import current_user

from .accounts import ADMIN_ROLE, PERMISSIONS, Accounts, clean_perms

DENIED = {"error": "you don't have permission for this"}
USER_ACTIONS = {"user-add", "user-role", "user-disable", "user-password", "user-remove"}
ROLE_ACTIONS = {"role-add", "role-update", "role-remove", "library-access"}


class AdminAPI:
    def __init__(self, accounts: Accounts, library=None):
        self.accounts = accounts
        self.library = library

    def _libraries(self, me) -> list[dict]:
        """The libraries ``me`` can see, each with its access rule."""
        from .perms import hidden_libraries

        if self.library is None:
            return []
        rules, libs = self.accounts.library_rules(), self.library.libraries(counts=False)
        hidden = hidden_libraries(rules, me)
        open_rule = {"roles": None, "guests": True}
        return [{**x, **rules.get(x["id"], open_rule)} for x in libs if x["id"] not in hidden]

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
        if "roles.manage" in perms and self.library is not None:
            out["admin_libraries"] = self._libraries(me)
        return out

    def get(self, action: str, query: dict):
        import json

        def out(status, obj):
            return status, {"Content-Type": "application/json"}, json.dumps(obj).encode()

        me = current_user()
        if not me:
            return out(401, {"error": "sign in first"})
        if action == "audit":
            if "users.manage" not in me["perms"]:
                return out(403, DENIED)
            return out(
                200,
                self.accounts.audit_log(
                    int(query.get("limit", 100)), int(query.get("offset", 0)), query.get("action", "")
                ),
            )
        return out(404, {"error": "not found"})

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
                user = acc.add(str(body.get("name", "")), str(body.get("password", "")), role=role["id"])
                acc.audit("user added", user["name"], f"role {role['name']}", actor=me)
                return 200, user
            if action == "user-role":
                user, role = target(), role_to_give()
                result = acc.set_role(user["name"], role["id"])
                acc.audit("role given", user["name"], f"{user['role']} → {role['name']}", actor=me)
                return 200, result
            if action == "user-disable":
                user = target()
                if user["id"] == me["id"]:
                    raise ValueError("you can't disable your own account")
                disabled = body.get("disabled", True)
                if not isinstance(disabled, bool):
                    raise ValueError("disabled must be true or false")
                acc.set_disabled(user["name"], disabled)
                acc.audit("user disabled" if disabled else "user enabled", user["name"], actor=me)
                return 200, acc.get_id(user["id"])
            if action == "user-password":
                user = target()
                acc.set_password(user["name"], str(body.get("password", "")))  # signs them out everywhere
                acc.audit("password reset", user["name"], "signed out everywhere", actor=me)
                return 200, {"ok": True}
            if action == "user-remove":
                user = target()
                if user["id"] == me["id"]:
                    raise ValueError("you can't remove your own account")
                acc.remove(user["name"])
                acc.audit("user removed", user["name"], actor=me)
                return 200, {"ok": True}
            if action == "role-add":
                perms = clean_perms(body.get("perms") or [])  # unknown names: 400 first
                if not within(perms):
                    raise PermissionError("you can't give permissions you don't have")
                role = acc.add_role(str(body.get("name", "")), perms)
                acc.audit("role added", role["name"], ", ".join(role["perms"]) or "no permissions", actor=me)
                return 200, role
            if action == "role-update":
                role = acc.role(int(body["id"]))
                perms = body.get("perms")
                perms = None if perms is None else clean_perms(perms)
                if not role["locked"] and not within(role["perms"]):
                    raise PermissionError("that role has permissions you don't have")
                if perms is not None and not within(perms):
                    raise PermissionError("you can't give permissions you don't have")
                result = acc.update_role(role["id"], body.get("name"), perms)
                changes = []
                if result["name"] != role["name"]:
                    changes.append(f"renamed from {role['name']}")
                if perms is not None:
                    added = [x for x in result["perms"] if x not in role["perms"]]
                    taken = [x for x in role["perms"] if x not in result["perms"]]
                    changes += [f"+{x}" for x in added] + [f"-{x}" for x in taken]
                acc.audit("role changed", result["name"], " ".join(changes), actor=me)
                return 200, result
            if action == "library-access":
                lid, roles, guests = int(body["id"]), body.get("roles"), body.get("guests", True)
                if self.library is None:
                    return 404, {"error": "there is no library"}
                current = next((x for x in self._libraries(me) if x["id"] == lid), None)
                if current is None:
                    raise KeyError(f"no library {lid}")
                if not me["admin"]:
                    every = {r["id"] for r in acc.roles()} - {ADMIN_ROLE}  # Admin always sees all

                    def seen(x):
                        return every if x is None else set(x) - {ADMIN_ROLE}

                    for rid in seen(current["roles"]) ^ seen(roles if isinstance(roles, list) else None):
                        if rid in every and not within(acc.role(rid)["perms"]):
                            raise PermissionError("that changes a role with permissions you don't have")
                result = acc.set_library_access(lid, roles, guests)
                acc.forget_libraries({x["id"] for x in self.library.libraries(counts=False)})
                names = {r["id"]: r["name"] for r in acc.roles()}
                who = (
                    "every role" if result["roles"] is None else ", ".join(names[r] for r in result["roles"])
                )
                acc.audit(
                    "library access",
                    current["name"],
                    f"{who or 'Admin only'}; guests {'yes' if guests else 'no'}",
                    actor=me,
                )
                return 200, result
            if action == "role-remove":
                role = acc.role(int(body["id"]))
                if not role["locked"] and not within(role["perms"]):
                    raise PermissionError("that role has permissions you don't have")
                acc.remove_role(role["id"])
                acc.audit("role removed", role["name"], actor=me)
                return 200, {"ok": True}
        except PermissionError as e:
            return 403, {"error": str(e)}
        except KeyError as e:
            return 404, {"error": str(e).strip("'\"")}
        return 404, {"error": "not found"}
