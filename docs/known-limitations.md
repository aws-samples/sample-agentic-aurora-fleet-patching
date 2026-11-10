# Known Limitations and Workarounds

[Back to README](../README.md)


Read these before running upgrades.

| Limitation | What you see | Workaround |
|---|---|---|
| **Tag every cluster with `Environment`.** The environment (`production`, `staging`, `dev`) sets the change window. Untagged clusters are guessed from their name, and anything not recognized is treated as `dev`, which has no change window. | An untagged production cluster can be upgraded at any time. | Add the tag `Environment=production`, `staging`, or `dev` to each cluster. |
| **Default change windows:** production Sat–Sun 04:00–07:00 UTC, staging Mon–Fri 02:00–08:00 UTC, dev anytime. | The Change Window check fails and the upgrade is blocked. | Set the windows in the **Maintenance** tab, or select **Skip window check** in the guardrails dialog. |
| **Check the upgrade target.** When a cluster is already on the newest minor version, the suggested target can be the newest **major** version (for example, 15.19 → 18.6). | The guardrails dialog header shows a major-version jump. | Read the version in the dialog header before you choose **Proceed with Upgrade**. Use **Version Changes** in the cluster panel to review a major upgrade first. |
| **Schema changes cancel Blue/Green switchovers.** Blue/Green replicates with logical replication, which doesn't carry DDL. Any DDL on the source cluster after the green environment is created, even `CREATE ... IF NOT EXISTS`, makes RDS cancel the switchover, and the deployment must be deleted and created again. The agent's own tools (the traffic generator and **Capture Baseline**) only run DDL when their objects don't exist yet, but your application's migrations count too. | RDS event: "Switchover ... was cancelled because there are DDL or Large Object changes ... that can't be replicated". | Pause schema migrations on the source cluster until the switchover completes. Enable the traffic generator and capture baselines *before* creating a Blue/Green deployment, not during it. |
| **Major-version Blue/Green uses the default parameter group on green.** For minor upgrades the green cluster keeps your custom cluster parameter group; for a major upgrade it can't (the parameter-group family changes), so green gets the new family's default group. | After switchover, the cluster shows `default.aurora-...`. | Before switchover, **Modify** the green cluster in the RDS console to use a custom parameter group for the new family (apply immediately) and reboot the green writer. |
| **Aurora MySQL CVE data is a static list.** PostgreSQL CVEs come from the NVD API; MySQL uses a built-in list of 8 CVEs. | MySQL clusters are only flagged for CVEs on that list. | Check the [Aurora MySQL release notes](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/AuroraMySQL.Updates.30Updates.html) for newer security fixes. |
| **AWS Health events need Business or Enterprise Support.** | Logs show `SubscriptionRequiredException`; scoring skips Health events. | None needed; CVE and end-of-life scoring still work. |
