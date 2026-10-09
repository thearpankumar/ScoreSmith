"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { AlertTriangle, KeyRound, Loader2, Pencil, Plus, Power, PowerOff, Search, Trash2 } from "lucide-react";

import { GlassCard } from "@/components/design-system/GlassCard";
import { SolidPanel } from "@/components/design-system/SolidPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { validateEmail, validateName, validateNewPassword } from "@/lib/auth-helpers";
import {
  createAdminUser,
  deleteAdminUser,
  listAdminUsers,
  setAdminUserActive,
  setAdminUserPassword,
  updateAdminUser,
  type AdminUser,
} from "@/lib/collab-client";
import { cn, formatDate } from "@/lib/utils";

type Dialogs =
  | { kind: "create" }
  | { kind: "edit"; user: AdminUser }
  | { kind: "password"; user: AdminUser }
  | { kind: "toggle"; user: AdminUser }
  | { kind: "delete"; user: AdminUser }
  | null;

const PAGE = 50;

/**
 * Administrator-only user management (the server re-checks the role on every call; non-admins never reach this page).
 * Responsive: a table from `md` up, stacked cards below. Destructive actions (deactivate, delete) ask for confirmation
 * and explain their consequences.
 */
export function UsersAdminClient({ currentUserId }: { currentUserId: string }) {
  const [q, setQ] = useState("");
  const [role, setRole] = useState("all");
  const [active, setActive] = useState("all");
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<Dialogs>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(
    async (append: boolean) => {
      const mine = ++seq.current;
      if (append) setLoadingMore(true);
      else setLoading(true);
      try {
        const page = await listAdminUsers({ q, role, active, skip: append ? users.length : 0, limit: PAGE });
        if (mine !== seq.current) return;
        setUsers((prev) => (append ? [...prev, ...page.items.filter((u) => !prev.some((p) => p.id === u.id))] : page.items));
        setTotal(page.total);
        setError(null);
      } catch (err) {
        if (mine === seq.current) setError(err instanceof Error ? err.message : "Could not load users.");
      } finally {
        if (mine === seq.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [q, role, active, users.length],
  );

  // Reload (debounced for typing) when a filter changes.
  useEffect(() => {
    const t = setTimeout(() => void load(false), q ? 250 : 0);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, role, active]);

  const done = (message: string) => {
    setDialog(null);
    setFlash(message);
    void load(false);
  };

  return (
    <div className="flex flex-col gap-4">
      {/* md:pr-14 leaves room for the notification bell that floats at the top right of the page */}
      <div className="md:pr-14">
        <h1 className="text-2xl font-semibold text-ink">Users</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Create accounts, change roles and passwords, deactivate or delete people. Admins manage accounts only: they
          cannot read other people&apos;s private charts or chats.
        </p>
      </div>

      <GlassCard elevation={1} className="flex flex-wrap items-center gap-3 p-3">
        <div className="relative min-w-52 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-ink-muted" aria-hidden />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search name, email or username…"
            aria-label="Search users"
            className="min-h-11 bg-solid pl-9"
          />
        </div>
        <Select label="Role" value={role} onChange={setRole} options={[["all", "All roles"], ["admin", "Admins"], ["user", "Users"]]} />
        <Select label="Status" value={active} onChange={setActive} options={[["all", "Any status"], ["active", "Active"], ["inactive", "Deactivated"]]} />
      </GlassCard>

      <div aria-live="polite" role="status">
        {flash && <p className="rounded-lg bg-lemon-soft px-3 py-2 text-sm text-lemon-ink">{flash}</p>}
      </div>
      {error && (
        <p role="alert" className="flex items-start gap-1.5 text-sm text-[var(--rag-poor)]">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          {error}
        </p>
      )}

      {/* The count and "Add user" share one line above the table; always rendered so Add user is reachable while
          loading and when no user matches the filters. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-ink-muted">{!loading && users.length > 0 ? `Showing ${users.length} of ${total}` : ""}</p>
        <Button type="button" className="min-h-11" onClick={() => setDialog({ kind: "create" })}>
          <Plus aria-hidden /> Add user
        </Button>
      </div>

      {loading ? (
        <p className="flex items-center gap-2 py-8 text-sm text-ink-muted">
          <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden /> Loading users…
        </p>
      ) : users.length === 0 ? (
        <SolidPanel className="p-6 text-sm text-ink-muted">No users match these filters.</SolidPanel>
      ) : (
        <>
          {/* md and up: a real table */}
          <SolidPanel className="hidden overflow-x-auto md:block">
            <table className="w-full text-left text-sm" data-testid="users-table">
              <thead className="text-xs uppercase tracking-wide text-ink-muted">
                <tr>
                  <th scope="col" className="px-4 py-3">Name</th>
                  <th scope="col" className="px-4 py-3">Username</th>
                  <th scope="col" className="px-4 py-3">Role</th>
                  <th scope="col" className="px-4 py-3">Status</th>
                  <th scope="col" className="px-4 py-3">Created</th>
                  <th scope="col" className="px-4 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline">
                {users.map((u) => (
                  <tr key={u.id} className={cn(!u.isActive && "opacity-70")}>
                    <td className="px-4 py-3">
                      <p className="font-medium text-ink">{u.name}</p>
                      <p className="break-all text-xs text-ink-muted">{u.email}</p>
                    </td>
                    <td className="px-4 py-3 text-ink-muted">{u.username ? `@${u.username}` : "—"}</td>
                    <td className="px-4 py-3"><RoleBadge role={u.role} /></td>
                    <td className="px-4 py-3"><StatusBadge active={u.isActive} /></td>
                    <td className="px-4 py-3 text-xs text-ink-muted" suppressHydrationWarning>{formatDate(u.createdAt)}</td>
                    <td className="px-4 py-3"><Actions user={u} self={u.id === currentUserId} onAction={setDialog} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </SolidPanel>
          {/* below md: cards */}
          <ul className="flex flex-col gap-3 md:hidden" data-testid="users-cards">
            {users.map((u) => (
              <li key={u.id}>
                <SolidPanel className={cn("flex flex-col gap-3 p-4", !u.isActive && "opacity-70")}>
                  <div>
                    <p className="break-words font-medium text-ink">{u.name}</p>
                    <p className="break-all text-xs text-ink-muted">{u.email}</p>
                    {u.username && <p className="text-xs text-ink-muted">@{u.username}</p>}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <RoleBadge role={u.role} />
                    <StatusBadge active={u.isActive} />
                    <span className="text-xs text-ink-muted" suppressHydrationWarning>Since {formatDate(u.createdAt)}</span>
                  </div>
                  <Actions user={u} self={u.id === currentUserId} onAction={setDialog} />
                </SolidPanel>
              </li>
            ))}
          </ul>
          {users.length < total && (
            <Button type="button" variant="outline" className="min-h-11 self-center" disabled={loadingMore} onClick={() => void load(true)}>
              {loadingMore && <Loader2 className="animate-spin" aria-hidden />} Load more
            </Button>
          )}
        </>
      )}

      {dialog?.kind === "create" && (
        <UserForm key="create" onClose={() => setDialog(null)} onDone={(u) => done(`Created ${u.name}.`)} />
      )}
      {dialog?.kind === "edit" && (
        <UserForm key={dialog.user.id} user={dialog.user} self={dialog.user.id === currentUserId} onClose={() => setDialog(null)} onDone={(u) => done(`Saved ${u.name}.`)} />
      )}
      {dialog?.kind === "password" && (
        <PasswordForm user={dialog.user} onClose={() => setDialog(null)} onDone={() => done(`Password changed for ${dialog.user.name}. They were signed out everywhere.`)} />
      )}
      {dialog?.kind === "toggle" && (
        <Confirm
          title={dialog.user.isActive ? `Deactivate ${dialog.user.name}?` : `Reactivate ${dialog.user.name}?`}
          body={
            dialog.user.isActive
              ? "They are signed out everywhere immediately and cannot sign in. Their running jobs are cancelled. Their charts and evaluations stay as they are."
              : "They can sign in again with their current password."
          }
          confirm={dialog.user.isActive ? "Deactivate" : "Reactivate"}
          destructive={dialog.user.isActive}
          onClose={() => setDialog(null)}
          action={async () => {
            await setAdminUserActive(dialog.user.id, !dialog.user.isActive);
            done(dialog.user.isActive ? `${dialog.user.name} was deactivated.` : `${dialog.user.name} was reactivated.`);
          }}
        />
      )}
      {dialog?.kind === "delete" && (
        <Confirm
          title={`Delete ${dialog.user.name}?`}
          body="The account is anonymised and can never sign in again. Their private charts and chats are deleted. Charts that are shared with others move to a collaborator. Evaluations they ran on other people's charts stay, labelled “Deleted user”. This cannot be undone."
          confirm="Delete user"
          destructive
          onClose={() => setDialog(null)}
          action={async () => {
            await deleteAdminUser(dialog.user.id);
            done(`${dialog.user.name} was deleted.`);
          }}
        />
      )}
    </div>
  );
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: Array<[string, string]> }) {
  return (
    <label className="flex items-center text-xs font-medium text-ink-muted">
      <span className="sr-only">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className="h-11 glass-field rounded-lg px-2.5 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--focus)]"
      >
        {options.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
    </label>
  );
}

function RoleBadge({ role }: { role: string }) {
  return <Badge variant={role === "admin" ? "lemon" : "outline"}>{role === "admin" ? "Admin" : "User"}</Badge>;
}

function StatusBadge({ active }: { active: boolean }) {
  return <Badge variant={active ? "outline" : "muted"}>{active ? "Active" : "Deactivated"}</Badge>;
}

function Actions({ user, self, onAction }: { user: AdminUser; self: boolean; onAction: (d: Dialogs) => void }) {
  const btn = "min-h-11 md:min-h-9";
  return (
    <div className="flex flex-wrap justify-end gap-1.5 max-md:justify-start">
      <Button type="button" variant="outline" size="sm" className={btn} onClick={() => onAction({ kind: "edit", user })} aria-label={`Edit ${user.name}`}>
        <Pencil aria-hidden /> Edit
      </Button>
      <Button type="button" variant="outline" size="sm" className={btn} onClick={() => onAction({ kind: "password", user })} aria-label={`Reset password of ${user.name}`}>
        <KeyRound aria-hidden /> Password
      </Button>
      {!self && (
        <>
          <Button type="button" variant="outline" size="sm" className={btn} onClick={() => onAction({ kind: "toggle", user })} aria-label={`${user.isActive ? "Deactivate" : "Reactivate"} ${user.name}`}>
            {user.isActive ? <PowerOff aria-hidden /> : <Power aria-hidden />} {user.isActive ? "Deactivate" : "Reactivate"}
          </Button>
          <Button type="button" variant="ghost" size="sm" className={cn(btn, "text-[var(--rag-poor)] hover:bg-[var(--rag-poor)]/10")} onClick={() => onAction({ kind: "delete", user })} aria-label={`Delete ${user.name}`}>
            <Trash2 aria-hidden /> Delete
          </Button>
        </>
      )}
    </div>
  );
}

function Shell({ title, description, children, onClose }: { title: string; description?: string; children: ReactNode; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90dvh] w-[calc(100vw-1.5rem)] overflow-y-auto p-4 sm:p-6">
        <DialogHeader className="pr-6">
          <DialogTitle>{title}</DialogTitle>
          {description && <DialogDescription>{description}</DialogDescription>}
        </DialogHeader>
        {children}
      </DialogContent>
    </Dialog>
  );
}

function UserForm({ user, self, onClose, onDone }: { user?: AdminUser; self?: boolean; onClose: () => void; onDone: (u: AdminUser) => void }) {
  const editing = Boolean(user);
  const [name, setName] = useState(user?.name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [username, setUsername] = useState(user?.username ?? "");
  const [role, setRole] = useState<"admin" | "user">(user?.role ?? "user");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validateName(name) ?? validateEmail(email) ?? (editing ? null : validateNewPassword(password, email));
    if (problem) return setError(problem);
    setBusy(true);
    setError(null);
    try {
      const saved = editing
        ? await updateAdminUser(user!.id, { name, email, username: username || null, ...(self ? {} : { role }) })
        : await createAdminUser({ name, email, username, role, password });
      onDone(saved);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the user.");
      setBusy(false);
    }
  }

  return (
    <Shell title={editing ? `Edit ${user!.name}` : "Add a user"} description={editing ? "Changing the role signs the person out so it takes effect at the next sign-in." : "They can sign in right away with this password."} onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <Field id="u-name" label="Full name"><Input id="u-name" value={name} onChange={(e) => setName(e.target.value)} className="min-h-11" autoComplete="off" /></Field>
        <Field id="u-email" label="Email"><Input id="u-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} className="min-h-11" autoComplete="off" /></Field>
        <Field id="u-username" label="Username (optional)"><Input id="u-username" value={username} onChange={(e) => setUsername(e.target.value)} className="min-h-11" autoComplete="off" autoCapitalize="none" placeholder="letters, digits, . _ -" /></Field>
        <Field id="u-role" label="Role">
          <select id="u-role" value={role} disabled={self} onChange={(e) => setRole(e.target.value as "admin" | "user")} className="h-11 glass-field w-full rounded-lg px-2.5 text-sm text-ink disabled:opacity-60">
            <option value="user">User - Charts, Chat, Evaluations</option>
            <option value="admin">Admin - also Users and Settings</option>
          </select>
          {self && <span className="text-xs text-ink-muted">You cannot change your own role.</span>}
        </Field>
        {!editing && (
          <Field id="u-pass" label="Initial password">
            <Input id="u-pass" type="text" value={password} onChange={(e) => setPassword(e.target.value)} className="min-h-11 font-mono" autoComplete="off" />
            <span className="text-xs text-ink-muted">At least 12 characters (14+ for admins). Share it securely; they can change it in Account.</span>
          </Field>
        )}
        {error && <p role="alert" className="text-sm text-[var(--rag-poor)]">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" className="min-h-11" onClick={onClose}>Cancel</Button>
          <Button type="submit" className="min-h-11" disabled={busy}>
            {busy && <Loader2 className="animate-spin" aria-hidden />} {editing ? "Save changes" : "Create user"}
          </Button>
        </DialogFooter>
      </form>
    </Shell>
  );
}

function PasswordForm({ user, onClose, onDone }: { user: AdminUser; onClose: () => void; onDone: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validateNewPassword(password, user.email);
    if (problem) return setError(problem);
    setBusy(true);
    try {
      await setAdminUserPassword(user.id, password);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change the password.");
      setBusy(false);
    }
  }
  return (
    <Shell title={`Reset password for ${user.name}`} description="They are signed out everywhere and must use the new password." onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-3" noValidate>
        <Field id="p-new" label="New password"><Input id="p-new" type="text" value={password} onChange={(e) => setPassword(e.target.value)} className="min-h-11 font-mono" autoComplete="off" /></Field>
        {error && <p role="alert" className="text-sm text-[var(--rag-poor)]">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" className="min-h-11" onClick={onClose}>Cancel</Button>
          <Button type="submit" className="min-h-11" disabled={busy || !password}>{busy && <Loader2 className="animate-spin" aria-hidden />} Set password</Button>
        </DialogFooter>
      </form>
    </Shell>
  );
}

function Confirm({ title, body, confirm, destructive, action, onClose }: { title: string; body: string; confirm: string; destructive?: boolean; action: () => Promise<void>; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent
        className="w-[calc(100vw-1.5rem)] p-4 sm:p-6"
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          cancel.current?.focus(); // Cancel is the default so Enter can never confirm a destructive action
        }}
      >
        <DialogHeader className="pr-6">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-[var(--rag-poor)]">{error}</p>}
        <DialogFooter>
          <Button ref={cancel} type="button" variant="ghost" className="min-h-11" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button
            type="button"
            variant={destructive ? "destructive" : "default"}
            className="min-h-11"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await action();
              } catch (err) {
                setError(err instanceof Error ? err.message : "Something went wrong.");
                setBusy(false);
              }
            }}
          >
            {busy && <Loader2 className="animate-spin" aria-hidden />} {confirm}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
    </div>
  );
}
