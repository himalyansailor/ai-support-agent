"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { can, canAny, PERMISSIONS } from "@/lib/permissions";
import { useOrg } from "@/components/app-shell";
import { InlineError, errorMessage } from "@/components/inline-error";
import { LoadingState } from "@/components/states";
import {
  browserTimezone,
  channelKeys,
  channelLabel,
  parseChannelParam,
  parseRangeParams,
  rangeFromPreset,
  rollupNotice,
} from "@/lib/analytics";
import { RangePicker } from "@/components/analytics/range-picker";
import { ChannelFilter } from "@/components/analytics/channel-filter";
import { KpiCards } from "@/components/analytics/kpi-cards";
import { OpenQueue } from "@/components/analytics/open-queue";
import { OutcomeChart, VolumeChart } from "@/components/analytics/volume-chart";
import { TimesChart } from "@/components/analytics/times-chart";
import { CsatCard } from "@/components/analytics/csat-card";
import { AgentTable } from "@/components/analytics/agent-table";
import { ReasonsList } from "@/components/analytics/reasons-list";
import { KnowledgeGaps } from "@/components/analytics/knowledge-gaps";
import { UsageCard } from "@/components/analytics/usage-card";
import { UsageMeters } from "@/components/billing/usage-meters";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

// GET `path` (not fetched while `enabled` is false). The previous result stays in `data` while a new path loads
// (`loading`), so a range change does not blank the page; `error` belongs to the current path only.
function useGet(api, path, enabled = true) {
  const [state, setState] = useState({ path: null, data: null, error: null });
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api
      .get(path)
      .then((data) => !cancelled && setState({ path, data, error: null }))
      .catch((err) => !cancelled && setState({ path, data: null, error: errorMessage(err) }));
    return () => {
      cancelled = true;
    };
  }, [api, path, enabled]);
  const current = state.path === path;
  return { data: state.data, error: current ? state.error : null, loading: enabled && !current };
}

function Section({ result, children }) {
  if (result.error) return <InlineError message={result.error} />;
  if (!result.data) return <p className="text-sm text-muted-foreground">Loading...</p>;
  return <div className={result.loading ? "opacity-60 transition-opacity" : undefined}>{children(result.data)}</div>;
}

function AnalyticsDashboard() {
  const { orgId, membership } = useOrg();
  const { api, user } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const urlRange = parseRangeParams(params.get("from"), params.get("to"));
  const rangeError = urlRange.error; // refused client-side, nothing is fetched for a bad range
  const channel = parseChannelParam(params.get("channel"));
  // Without a range in the URL the API's default (the last 7 days, in the org timezone) applies.
  const rangeQs = urlRange.from && !rangeError ? new URLSearchParams({ from: urlRange.from, to: urlRange.to }).toString() : "";
  const suffix = rangeQs ? `?${rangeQs}` : "";
  const ok = !rangeError;

  const overviewQs = new URLSearchParams(rangeQs);
  if (channel) overviewQs.set("channel", channel);
  const overviewSuffix = overviewQs.size ? `?${overviewQs}` : "";
  const overview = useGet(api, `/orgs/${orgId}/analytics/overview${overviewSuffix}`, ok);
  const agents = useGet(api, `/orgs/${orgId}/analytics/agents${suffix}`, ok);
  const escalations = useGet(api, `/orgs/${orgId}/analytics/reasons?kind=escalation${rangeQs ? `&${rangeQs}` : ""}`, ok);
  const topics = useGet(api, `/orgs/${orgId}/analytics/reasons?kind=topic${rangeQs ? `&${rangeQs}` : ""}`, ok);
  const usage = useGet(api, `/orgs/${orgId}/analytics/usage${suffix}`, ok);
  // Only needed for the picker when the overview (which carries the timezone) is not there yet.
  const settings = useGet(api, `/orgs/${orgId}/support-settings`, !overview.data);
  // Plan usage comes from the billing summary, which only billing managers may read.
  const canSeePlan = can(membership, PERMISSIONS.BILLING_MANAGE);
  const planUsage = useGet(api, `/orgs/${orgId}/billing`, canSeePlan);
  const canSeeGaps = can(membership, PERMISSIONS.ANALYTICS_VIEW) && canAny(membership, [PERMISSIONS.BOT_CONFIGURE, PERMISSIONS.KB_MANAGE]);

  const update = useCallback(
    (changes) => {
      const next = new URLSearchParams(params.toString());
      for (const [k, v] of Object.entries(changes)) {
        if (v) next.set(k, v);
        else next.delete(k);
      }
      router.replace(`${pathname}?${next}`);
    },
    [params, pathname, router],
  );

  const ov = ok ? overview.data : null;
  const timezone = overview.data?.timezone ?? settings.data?.timezone ?? browserTimezone();
  // The picker shows whenever a range is known: the one in the URL (even a bad one, so it can be corrected) or the API's.
  const fallback = rangeError ? rangeFromPreset("7d", timezone) : null; // unusable URL dates: start the picker from 7 days
  const from = urlRange.from ?? ov?.from_date ?? fallback?.from ?? null;
  const to = urlRange.to ?? ov?.to_date ?? fallback?.to ?? null;
  const pageError = rangeError ?? overview.error;
  const failed = Boolean(pageError); // one range-level error; the sections below would only repeat it
  const lagNotice = ov ? rollupNotice(ov.rollup, ov.timezone) : null;

  return (
    <div className="flex flex-col gap-4" aria-busy={overview.loading}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        {from && to ? (
          <RangePicker timezone={timezone} from={from} to={to} onChange={({ from: f, to: t }) => update({ from: f, to: t })} />
        ) : (
          !failed && <p className="text-sm text-muted-foreground">Loading...</p>
        )}
        <ChannelFilter value={channel} seen={ov ? channelKeys(ov.series) : []} onChange={(c) => update({ channel: c })} />
      </div>
      <InlineError message={pageError} />
      {!failed && lagNotice && (
        <div role="status" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
          {lagNotice}
        </div>
      )}
      {!failed && ov && (
        <div className={`flex flex-col gap-4 ${overview.loading ? "opacity-60 transition-opacity" : ""}`}>
          <KpiCards totals={ov.totals} />
          <div className="grid gap-4 lg:grid-cols-2">
            <VolumeChart series={ov.series} interval={ov.interval} timezone={ov.timezone} />
            <OutcomeChart series={ov.series} interval={ov.interval} timezone={ov.timezone} />
            <TimesChart series={ov.series} totals={ov.totals} interval={ov.interval} timezone={ov.timezone} />
            <div className="flex flex-col gap-4">
              <OpenQueue queue={ov.open_queue} />
              <CsatCard totals={ov.totals} series={ov.series} interval={ov.interval} timezone={ov.timezone} />
            </div>
          </div>
        </div>
      )}
      {!failed && (
        <>
          {channel && (
            <p className="text-sm text-muted-foreground">
              The {channelLabel(channel)} filter applies to the numbers and charts above. Agents, reasons, AI usage and
              knowledge gaps below cover all channels.
            </p>
          )}
          <Section result={agents}>{(d) => <AgentTable items={d.items} />}</Section>
          <div className="grid gap-4 lg:grid-cols-2">
            <Section result={escalations}>{(d) => <ReasonsList title="Escalation reasons" items={d.items} />}</Section>
            <Section result={topics}>{(d) => <ReasonsList title="Top topics" items={d.items} />}</Section>
          </div>
          <Section result={usage}>{(d) => <UsageCard usage={d} isPlatformAdmin={Boolean(user?.is_platform_admin)} />}</Section>
          {canSeePlan && (
            <Section result={planUsage}>
              {(d) => (
                <Card>
                  <CardHeader>
                    <CardTitle>Usage vs plan</CardTitle>
                  </CardHeader>
                  <CardContent className="flex flex-col gap-3">
                    <p className="text-sm text-muted-foreground">
                      {d.plan ? d.plan.name : "No plan"}, for the current billing period.
                    </p>
                    <UsageMeters meters={d.meters} capacity={d.capacity} />
                  </CardContent>
                </Card>
              )}
            </Section>
          )}
          <KnowledgeGaps orgId={orgId} query={rangeQs} allowed={canSeeGaps} />
        </>
      )}
    </div>
  );
}

export default function DashboardPage() {
  const { orgId, membership } = useOrg();
  const { api } = useAuth();
  const [notReady, setNotReady] = useState(false);
  const canSeeBanner = canAny(membership, [PERMISSIONS.BOT_CONFIGURE, PERMISSIONS.BILLING_MANAGE]);
  const canSeeAnalytics = can(membership, PERMISSIONS.ANALYTICS_VIEW);

  useEffect(() => {
    if (!canSeeBanner) return;
    let cancelled = false;
    api
      .get(`/orgs/${orgId}/ai/settings`)
      .then((s) => !cancelled && setNotReady(s.ready === false))
      .catch(() => {}); // the banner is a hint; never block the dashboard on it
    return () => {
      cancelled = true;
    };
  }, [api, orgId, canSeeBanner]);

  return (
    <div className="flex flex-col gap-4">
      {canSeeBanner && notReady && (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900"
        >
          <span>Your bot can&apos;t answer customers until an AI key is added.</span>
          <Link href={`/orgs/${orgId}/settings/ai`} className="font-medium underline">
            Go to AI settings
          </Link>
        </div>
      )}
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">{membership.organisation_name}</h1>
        <p className="text-sm text-muted-foreground">You are signed in as {membership.role_name}.</p>
      </div>
      {canSeeAnalytics && (
        <Suspense fallback={<LoadingState />}>
          <AnalyticsDashboard />
        </Suspense>
      )}
    </div>
  );
}
