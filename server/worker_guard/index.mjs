import { fileURLToPath } from 'node:url';
import { writeFileSync } from 'node:fs';

const guardPath = fileURLToPath(new URL('./guard.ps1', import.meta.url));

export const name = 'workflow-worker-guard';
export const inject = ['shell'];
export function apply(ctx) {
  const shell = ctx.shell;
  if (typeof shell.argv !== 'function' || shell.sandboxMode !== 'workspace-write') {
    throw new Error('Workflow worker requires a sandboxed workspace-write PowerShell executor');
  }
  ctx.effect(() => {
    const original = shell.argv;
    shell.argv = function(spec) {
    const encoded = Buffer.from(spec.command,'utf8').toString('base64');
    const root = spec.sandboxPolicy?.workspaceRoot ?? spec.workdir;
    const escaped = value => String(value).replaceAll("'", "''");
      return original.call(shell,{...spec, command:`& '${escaped(guardPath)}' -EncodedCommand '${encoded}' -WorkspaceRoot '${escaped(root)}'`});
    };
    shell.workflowGuardInstalled = true;
    if (process.env.WF_WORKER_GUARD_READY && process.env.WF_WORKER_GUARD_NONCE) {
      writeFileSync(process.env.WF_WORKER_GUARD_READY,JSON.stringify({nonce:process.env.WF_WORKER_GUARD_NONCE,guard:true,mode:shell.sandboxMode}));
    }
    return () => { shell.argv=original; shell.workflowGuardInstalled=false; };
  });
}
