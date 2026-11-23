import { writeFileSync, renameSync } from "node:fs";

export default function (pi) {
    const report = process.env.CARD_PI_SESSION_REPORT;
    if (!report) return;

    const writeSession = (_event, ctx) => {
        const session = ctx.sessionManager.getSessionFile();
        const pending = report + ".tmp";
        writeFileSync(pending, JSON.stringify({ cwd: ctx.cwd, session }), { mode: 0o600 });
        renameSync(pending, report);
    };
    pi.on("session_start", writeSession);
    pi.on("session_shutdown", writeSession);
}
