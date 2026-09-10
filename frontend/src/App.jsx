import { useCallback, useEffect, useMemo, useState } from "react";
import { createPlan, getPlan, reviewPlan } from "./api";

const initialForm = {
  current_location: "Bengaluru, India",
  destination: "Kyoto, Japan",
  start_date: "10/11/2026",
  end_date: "17/11/2026",
  budget_min: 150000,
  budget_max: 300000,
  currency: "INR",
  interests: "temples, food, photography",
  travelers: 1,
  preferences: "public transport, central hotel",
  transport_modes: ["flight", "train", "bus"],
  allow_transport_connections: true,
  include_premium_fares: false,
};

const initialReview = {
  action: "approve",
  feedback: "",
  hotel_preference: "",
  day: 1,
  replacement_activities: "",
  note: "",
};

function reviewStateFor(plan) {
  const choices = plan.awaiting_input?.available_plan_choices || [];
  return { ...initialReview, action: choices.length ? "approve" : "reject" };
}

const transportModes = ["flight", "train", "bus", "ferry", "car"];

function list(value) {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function money(value, currency) {
  if (value === null || value === undefined) return "Not available";
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency,
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    return String(value) + " " + currency;
  }
}

function label(value) {
  if (value === "activity_sources") return "activity citations";
  return String(value || "").replaceAll("_", " ");
}

function displayDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  return match ? `${match[3]}/${match[2]}/${match[1]}` : value;
}

function apiDate(value) {
  const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value.trim());
  if (!match) throw new Error("Enter dates as DD/MM/YYYY.");
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  if (
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    throw new Error("Enter a valid date as DD/MM/YYYY.");
  }
  return `${match[3]}-${match[2]}-${match[1]}`;
}

function preferredScenario(plan) {
  const choices = plan.awaiting_input?.available_plan_choices || [];
  if (choices.includes("within_budget")) return "within_budget";
  if (choices.includes("stretch")) return "stretch";
  return plan.draft_plan ? "within_budget" : "stretch";
}

function StatusBadge({ value }) {
  const warningValues = [
    "warning",
    "needs_verification",
    "historical_climate_estimate",
    "partial_dates",
    "dates_not_shown",
    "primary_leg_only",
    "awaiting_review",
  ];
  const errorValues = ["blocker", "failed"];
  const tone = errorValues.includes(value)
    ? "error"
    : warningValues.includes(value)
      ? "warning"
      : "success";
  return (
    <span className={"status-text status-" + tone}>
      <span aria-hidden="true" />
      {label(value)}
    </span>
  );
}

function ResearchView({ research }) {
  if (!research) return null;
  const weather = research.weather;
  const sources = [...(research.search_results || []), ...(research.transport_search_results || [])];
  const uniqueSources = Array.from(
    new Map(sources.filter((source) => source.url).map((source) => [source.url, source])).values(),
  );

  return (
    <section className="result-section">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">Research complete</p>
          <h2>What the planner found</h2>
        </div>
        <StatusBadge value={weather.data_type} />
      </div>

      <p className="research-summary">{research.summary}</p>

      <div className="weather-card">
        <div>
          <span className="muted-label">Weather context</span>
          <strong>{weather.summary}</strong>
        </div>
        <div className="weather-values">
          <span>High {weather.average_high_c ?? "—"}°C</span>
          <span>Low {weather.average_low_c ?? "—"}°C</span>
          <span>Rain chance {weather.precipitation_probability_max ?? "—"}%</span>
        </div>
      </div>

      <div className="transport-research">
        <h3>Transport research</h3>
        <p>{research.transport_summary}</p>
        <span>
          {research.transport_options.length} verified options ·{" "}
          {research.transport_search_results.length} transport sources checked
        </span>
      </div>

      <div className="research-columns">
        <InfoList title="Places and ideas" items={research.attractions} />
        <InfoList title="Local tips" items={research.local_tips} />
        <InfoList title="Safety notes" items={research.safety_notes} />
      </div>

      {uniqueSources.length > 0 && (
        <details className="source-box">
          <summary>View {uniqueSources.length} research sources</summary>
          <div className="source-list">
            {uniqueSources.map((source) => (
              <a href={source.url} key={source.url} target="_blank" rel="noreferrer">
                <strong>{source.title}</strong>
                <span>{source.source}</span>
              </a>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}

function InfoList({ title, items = [] }) {
  return (
    <div className="info-list">
      <h3>{title}</h3>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function ReadinessView({ readiness }) {
  if (!readiness) return null;
  return (
    <section className="readiness-section">
      <div className="readiness-heading">
        <div>
          <h2>Plan checks</h2>
          <p>{readiness.summary}</p>
        </div>
        <div className="score-value">
          <strong>{readiness.score}</strong>
          <span>/100</span>
        </div>
      </div>

      <div className="readiness-meta">
        <StatusBadge value={readiness.status} />
        <span>{readiness.warning_count} warnings</span>
        <span>{readiness.blocker_count} blockers</span>
      </div>

      <div className="check-list">
        {readiness.checks.map((check) => (
          <article className={"check-row check-" + check.status} key={check.name}>
            <div className="check-copy">
              <StatusBadge value={check.status} />
              <div>
                <strong>{label(check.name)}</strong>
                <p>{check.message}</p>
              </div>
            </div>
            <span>
              {check.awarded_points} / {check.weight} points
            </span>
          </article>
        ))}
      </div>
    </section>
  );
}

function TransportView({ option }) {
  if (!option) return null;
  return (
    <section className="subsection">
      <div className="section-title-row compact">
        <div>
          <p className="eyebrow">Selected transport</p>
          <h3>{option.route}</h3>
        </div>
        <strong className="price">
          {money(option.priced_total_cost, option.currency)}
        </strong>
      </div>
      <div className="transport-meta">
        <span>Price: {label(option.price_scope)}</span>
        <span>Fare dates: {label(option.fare_date_basis)}</span>
        <span>Cabin: {label(option.cabin_class)}</span>
      </div>

      <div className="leg-list">
        {(option.priced_legs || []).map((leg, index) => (
          <article className="leg-card" key={index}>
            <span className="leg-mode">{leg.mode}</span>
            <div>
              <strong>
                {leg.origin} → {leg.destination}
              </strong>
              <p>{leg.price_evidence}</p>
            </div>
            <strong>{money(leg.estimated_total_in_budget_currency, leg.budget_currency)}</strong>
          </article>
        ))}
        {(option.unpriced_legs || []).map((leg, index) => (
          <article className="leg-card leg-unpriced" key={"unpriced-" + index}>
            <span className="leg-mode">{leg.mode}</span>
            <div>
              <strong>
                {leg.origin} → {leg.destination}
              </strong>
              <p>{leg.reason}</p>
            </div>
            <StatusBadge value="warning" />
          </article>
        ))}
      </div>
    </section>
  );
}

function PlanView({ plan }) {
  if (!plan) return null;
  const budgetItems = [
    ["Transport", plan.budget.origin_transport],
    ["Lodging", plan.budget.lodging],
    ["Food", plan.budget.food],
    ["Activities", plan.budget.activities],
    ["Local travel", plan.budget.local_transport],
    ["Contingency", plan.budget.contingency],
  ];

  return (
    <section className="result-section">
      <div className="section-title-row">
        <div>
          <p className="eyebrow">{label(plan.scenario)} plan</p>
          <h2>{plan.title}</h2>
        </div>
        <div className="plan-total">
          <span>Total allocation</span>
          <strong>{money(plan.budget.total_budget, plan.budget.currency)}</strong>
        </div>
      </div>

      <TransportView option={plan.selected_transport} />

      <section className="subsection">
        <h3>Budget allocation</h3>
        <div className="budget-grid">
          {budgetItems.map(([name, value]) => (
            <div key={name}>
              <span>{name}</span>
              <strong>{money(value, plan.budget.currency)}</strong>
            </div>
          ))}
        </div>
      </section>

      <section className="subsection">
        <h3>Day-by-day itinerary</h3>
        <div className="day-list">
          {plan.days.map((day) => (
            <details className="day-card" key={day.day} open={day.day === 1}>
              <summary>
                <span>Day {day.day}</span>
                <strong>{day.theme}</strong>
                <time dateTime={day.date}>{displayDate(day.date)}</time>
              </summary>
              <div className="activity-list">
                {day.activities.map((activity, index) => (
                  <article key={index}>
                    <time>{activity.time}</time>
                    <div>
                      <strong>{activity.title}</strong>
                      <p>{activity.description}</p>
                    </div>
                    <span>{money(activity.estimated_cost, plan.budget.currency)}</span>
                  </article>
                ))}
              </div>
              {day.daily_notes.length > 0 && (
                <p className="day-note">{day.daily_notes.join(" ")}</p>
              )}
            </details>
          ))}
        </div>
      </section>

      <div className="two-column-details">
        <InfoList title="Packing list" items={plan.packing_list} />
        <InfoList title="Important assumptions" items={plan.assumptions} />
      </div>
    </section>
  );
}

function ReviewPanel({
  plan,
  selectedScenario,
  canApprove,
  review,
  setReview,
  onSubmit,
  loading,
}) {
  if (plan.status === "finalized") {
    return (
      <section className="review-section finalized-section">
        <div>
          <h2>Plan approved</h2>
          <p>The final itinerary is frozen and ready to retrieve.</p>
        </div>
        <StatusBadge value="finalized" />
      </section>
    );
  }

  if (plan.status !== "awaiting_review") return null;

  const hasPlan = Boolean(plan.draft_plan || plan.stretch_plan);
  const explicitBlockers = plan.awaiting_input?.blocking_issues || [];
  const selectedReadiness =
    selectedScenario === "stretch" ? plan.stretch_readiness : plan.draft_readiness;
  const readinessBlockers = (selectedReadiness?.checks || [])
    .filter((check) => check.status === "blocker")
    .map((check) => ({
      code: check.name,
      title: label(check.name),
      message: check.message,
      next_step: "Modify the plan or reject it and request new research.",
    }));
  const blockers = explicitBlockers.length
    ? explicitBlockers
    : readinessBlockers.length
      ? readinessBlockers
      : !hasPlan
        ? [
            {
              code: "plan_not_generated",
              title: "No plan could be generated",
              message:
                plan.research?.transport_summary ||
                "The required planning information was not available.",
              next_step:
                "Reject this result and request broader research, or create a new trip with different dates, transport modes, or budget.",
            },
          ]
        : [];
  const unavailableFields =
    plan.awaiting_input?.unavailable_fields ||
    (!hasPlan
      ? ["draft_plan", "draft_readiness", "stretch_plan", "stretch_readiness"]
      : []);
  const configuredActions = plan.awaiting_input?.allowed_actions || [
    "approve",
    "reject",
    "modify",
  ];
  const reviewActions = configuredActions.filter(
    (action) => (action !== "approve" || canApprove) && (action !== "modify" || hasPlan),
  );

  function update(name, value) {
    setReview((current) => ({ ...current, [name]: value }));
  }

  return (
    <form className="review-section" onSubmit={onSubmit}>
      <div className="section-title-row">
        <div>
          <h2>{hasPlan ? "Review this plan" : "Planning stopped"}</h2>
          <p>
            {hasPlan
              ? "Choose what should happen next."
              : "The research completed, but an itinerary could not be created."}
          </p>
        </div>
        <StatusBadge value={plan.status} />
      </div>

      {blockers.length > 0 && (
        <section className="review-blockers" aria-labelledby="review-blockers-title">
          <h3 id="review-blockers-title">
            {blockers.length === 1 ? "Approval blocker" : "Approval blockers"}
          </h3>
          {blockers.map((blocker) => (
            <div className="blocker-row" key={blocker.code}>
              <strong>{blocker.title}</strong>
              <p>{blocker.message}</p>
              <span>Next step: {blocker.next_step}</span>
            </div>
          ))}
          {unavailableFields.length > 0 && (
            <p className="unavailable-fields">
              Not generated: {unavailableFields.map(label).join(", ")}.
            </p>
          )}
        </section>
      )}

      <fieldset className="action-options">
        <legend>Decision</legend>
        {reviewActions.map((action) => (
          <label key={action}>
            <input
              type="radio"
              name="review-action"
              value={action}
              checked={review.action === action}
              disabled={action === "approve" && !canApprove}
              onChange={() => update("action", action)}
            />
            {action}
          </label>
        ))}
      </fieldset>

      {review.action === "approve" && (
        <p className="action-help">
          Approve the <strong>{label(selectedScenario)}</strong> plan shown above.
        </p>
      )}

      <label>
        {review.action === "reject" ? "Why should it be regenerated?" : "Feedback (optional)"}
        <textarea
          rows="3"
          value={review.feedback}
          onChange={(event) => update("feedback", event.target.value)}
        />
      </label>

      {review.action === "modify" && (
        <div className="modify-grid">
          <label className="wide-field">
            Hotel preference
            <input
              value={review.hotel_preference}
              onChange={(event) => update("hotel_preference", event.target.value)}
              placeholder="Quiet hotel near the station"
            />
          </label>
          <label>
            Day to change
            <input
              type="number"
              min="1"
              max={plan.draft_plan?.days.length || plan.stretch_plan?.days.length || 21}
              value={review.day}
              onChange={(event) => update("day", event.target.value)}
            />
          </label>
          <label>
            Replacement activities
            <input
              value={review.replacement_activities}
              onChange={(event) => update("replacement_activities", event.target.value)}
              placeholder="Tea ceremony, riverside walk"
            />
          </label>
          <label className="wide-field">
            Day note
            <input
              value={review.note}
              onChange={(event) => update("note", event.target.value)}
              placeholder="Keep the afternoon relaxed"
            />
          </label>
        </div>
      )}

      <button className={"review-submit action-" + review.action} disabled={loading}>
        {loading ? "Working…" : review.action + " plan"}
      </button>
    </form>
  );
}

export default function App() {
  const [form, setForm] = useState(initialForm);
  const [plan, setPlan] = useState(null);
  const [planId, setPlanId] = useState("");
  const [lookupId, setLookupId] = useState("");
  const [selectedScenario, setSelectedScenario] = useState("within_budget");
  const [review, setReview] = useState(initialReview);
  const [loading, setLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const scenarioPlan =
    selectedScenario === "stretch" ? plan?.stretch_plan : plan?.draft_plan;
  const scenarioReadiness =
    selectedScenario === "stretch" ? plan?.stretch_readiness : plan?.draft_readiness;

  const availableScenarios = useMemo(() => {
    if (!plan) return [];
    return [
      plan.draft_plan && ["within_budget", plan.draft_readiness],
      plan.stretch_plan && ["stretch", plan.stretch_readiness],
    ].filter(Boolean);
  }, [plan]);

  const approvableScenarios = plan?.awaiting_input?.available_plan_choices || [];
  const canApprove = approvableScenarios.includes(selectedScenario);

  function updateForm(name, value) {
    setForm((current) => ({ ...current, [name]: value }));
  }

  function toggleMode(mode) {
    setForm((current) => ({
      ...current,
      transport_modes: current.transport_modes.includes(mode)
        ? current.transport_modes.filter((item) => item !== mode)
        : [...current.transport_modes, mode],
    }));
  }

  const loadPlan = useCallback(async (id) => {
    const normalized = id.trim();
    if (!normalized) throw new Error("Enter a plan ID.");
    setLoading(true);
    setLoadingMessage("Loading saved plan…");
    setError("");
    setNotice("");
    try {
      const result = await getPlan(normalized);
      setPlan(result);
      setPlanId(normalized);
      setLookupId(normalized);
      setSelectedScenario(preferredScenario(result));
      setReview(reviewStateFor(result));
      return result;
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }, []);

  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return undefined;
    const lifecycle = new AbortController();
    Promise.resolve(
      context.registerTool(
        {
          name: "load_travel_plan",
          title: "Load travel plan",
          description: "Load an existing travel plan into the visible review workspace by ID.",
          inputSchema: {
            type: "object",
            properties: { planId: { type: "string", minLength: 1 } },
            required: ["planId"],
            additionalProperties: false,
          },
          annotations: { readOnlyHint: true, untrustedContentHint: true },
          async execute(input) {
            if (!input || typeof input.planId !== "string" || !input.planId.trim()) {
              throw new Error("planId is required");
            }
            const result = await loadPlan(input.planId);
            return {
              planId: result.plan_id,
              status: result.status,
              destination: result.request.destination,
            };
          },
        },
        { signal: lifecycle.signal },
      ),
    ).catch(() => {});
    return () => lifecycle.abort();
  }, [loadPlan]);

  async function handleCreate(event) {
    event.preventDefault();
    setError("");
    setNotice("");
    if (!form.transport_modes.length) {
      setError("Choose at least one transport mode.");
      return;
    }
    setLoading(true);
    setLoadingMessage("Researching and building your plan. This can take a minute…");
    try {
      const request = {
        ...form,
        start_date: apiDate(form.start_date),
        end_date: apiDate(form.end_date),
        budget_min: Number(form.budget_min),
        budget_max: Number(form.budget_max),
        travelers: Number(form.travelers),
        interests: list(form.interests),
        preferences: list(form.preferences),
      };
      const accepted = await createPlan(request);
      const result = await getPlan(accepted.plan_id);
      setPlan(result);
      setPlanId(accepted.plan_id);
      setLookupId(accepted.plan_id);
      setSelectedScenario(preferredScenario(result));
      setReview(reviewStateFor(result));
      setNotice(
        result.draft_plan || result.stretch_plan
          ? "Plan created. Review the evidence and readiness checks before deciding."
          : "Research completed, but planning stopped. Review the blocker and next step below.",
      );
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }

  async function handleLookup(event) {
    event.preventDefault();
    try {
      await loadPlan(lookupId);
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  async function handleReview(event) {
    event.preventDefault();
    setError("");
    setNotice("");
    const body = { action: review.action };
    if (review.feedback.trim()) body.feedback = review.feedback.trim();

    if (review.action === "approve") {
      if (!canApprove) {
        setError("This scenario has a blocker and cannot be approved.");
        return;
      }
      body.plan_choice = selectedScenario;
    }
    if (review.action === "reject" && !review.feedback.trim()) {
      setError("Feedback is required when rejecting a plan.");
      return;
    }
    if (review.action === "modify") {
      const activities = list(review.replacement_activities);
      const note = review.note.trim();
      const hotel = review.hotel_preference.trim();
      if (!hotel && !activities.length && !note) {
        setError("Add a hotel preference, replacement activities, or a day note.");
        return;
      }
      body.modifications = {};
      if (hotel) body.modifications.hotel_preference = hotel;
      if (activities.length || note) {
        body.modifications.day_changes = [
          {
            day: Number(review.day),
            replace_activities_with: activities,
            note: note || null,
          },
        ];
      }
    }

    setLoading(true);
    setLoadingMessage(
      review.action === "approve" ? "Finalizing plan…" : "Updating and rechecking the plan…",
    );
    try {
      const result = await reviewPlan(planId, body);
      setPlan(result);
      setReview(reviewStateFor(result));
      setNotice(
        review.action === "approve"
          ? "Plan approved and finalized."
          : "Plan updated. Review the new readiness result.",
      );
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
      setLoadingMessage("");
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Travel Planner</h1>
          <p>Create a trip, check the research, and review the result.</p>
        </div>
        <form className="plan-lookup" onSubmit={handleLookup}>
          <label htmlFor="plan-id">Open saved plan</label>
          <div>
            <input
              id="plan-id"
              value={lookupId}
              onChange={(event) => setLookupId(event.target.value)}
              placeholder="Plan ID"
            />
            <button disabled={loading}>Load</button>
          </div>
        </form>
      </header>

      {(error || notice) && (
        <div
          className={
            error
              ? "message error-message"
              : plan?.awaiting_input?.blocking_issues?.length
                ? "message warning-message"
                : "message notice-message"
          }
          role="status"
        >
          {error || notice}
        </div>
      )}

      <section className="workspace">
        <form className="panel planner-form" onSubmit={handleCreate}>
          <div className="section-heading">
            <div>
              <h2>Trip details</h2>
            </div>
          </div>

          <div className="field-grid">
            <TextField
              label="Starting from"
              value={form.current_location}
              onChange={(value) => updateForm("current_location", value)}
            />
            <TextField
              label="Destination"
              value={form.destination}
              onChange={(value) => updateForm("destination", value)}
            />
            <TextField
              label="Start date"
              value={form.start_date}
              placeholder="DD/MM/YYYY"
              inputMode="numeric"
              pattern="[0-9]{2}/[0-9]{2}/[0-9]{4}"
              onChange={(value) => updateForm("start_date", value)}
            />
            <TextField
              label="End date"
              value={form.end_date}
              placeholder="DD/MM/YYYY"
              inputMode="numeric"
              pattern="[0-9]{2}/[0-9]{2}/[0-9]{4}"
              onChange={(value) => updateForm("end_date", value)}
            />
            <TextField
              label="Minimum budget"
              type="number"
              min="0"
              value={form.budget_min}
              onChange={(value) => updateForm("budget_min", value)}
            />
            <TextField
              label="Maximum budget"
              type="number"
              min="1"
              value={form.budget_max}
              onChange={(value) => updateForm("budget_max", value)}
            />
            <TextField
              label="Currency"
              value={form.currency}
              maxLength="3"
              onChange={(value) => updateForm("currency", value.toUpperCase())}
            />
            <TextField
              label="Travelers"
              type="number"
              min="1"
              max="20"
              value={form.travelers}
              onChange={(value) => updateForm("travelers", value)}
            />
          </div>

          <TextField
            label="Interests"
            value={form.interests}
            placeholder="food, museums, hiking"
            onChange={(value) => updateForm("interests", value)}
          />
          <TextField
            label="Preferences"
            required={false}
            value={form.preferences}
            placeholder="public transport, vegetarian"
            onChange={(value) => updateForm("preferences", value)}
          />

          <fieldset>
            <legend>Transport modes</legend>
            <div className="check-row">
              {transportModes.map((mode) => (
                <label className="check-label" key={mode}>
                  <input
                    type="checkbox"
                    checked={form.transport_modes.includes(mode)}
                    onChange={() => toggleMode(mode)}
                  />
                  {mode}
                </label>
              ))}
            </div>
          </fieldset>

          <div className="option-stack">
            <label className="check-label">
              <input
                type="checkbox"
                checked={form.allow_transport_connections}
                onChange={(event) =>
                  updateForm("allow_transport_connections", event.target.checked)
                }
              />
              Allow connected routes
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={form.include_premium_fares}
                onChange={(event) =>
                  updateForm("include_premium_fares", event.target.checked)
                }
              />
              Include premium fares
            </label>
          </div>

          <button className="primary-button" disabled={loading}>
            {loading ? "Please wait…" : "Build trip plan"}
          </button>
        </form>

        <section className="results-column">
          {loading && (
            <section className="panel loading-panel">
              <span className="spinner" />
              <h2>{loadingMessage}</h2>
              <p>Keep this page open while the workflow completes.</p>
            </section>
          )}

          {!loading && !plan && (
            <section className="panel empty-panel">
              <h2>No plan yet</h2>
              <p>Enter the trip details and build a plan.</p>
            </section>
          )}

          {!loading && plan && (
            <>
              <section className="panel result-header">
                <div>
                  <p className="eyebrow">Plan {plan.plan_id.slice(0, 8)}</p>
                  <h2>
                    {plan.request.current_location} → {plan.request.destination}
                  </h2>
                </div>
                <StatusBadge value={plan.status} />
              </section>

              <section className="panel result-panel">
                <ResearchView research={plan.research} />
              </section>

              {availableScenarios.length > 0 && (
                <>
                  <section className="scenario-switch" aria-label="Budget scenario">
                    {availableScenarios.map(([scenario, readiness]) => (
                      <button
                        className={selectedScenario === scenario ? "active" : ""}
                        key={scenario}
                        type="button"
                        onClick={() => {
                          setSelectedScenario(scenario);
                          if (
                            !approvableScenarios.includes(scenario) &&
                            review.action === "approve"
                          ) {
                            setReview((current) => ({ ...current, action: "modify" }));
                          }
                        }}
                      >
                        <span>{label(scenario)}</span>
                        <strong>{readiness?.score ?? "—"}/100</strong>
                      </button>
                    ))}
                  </section>
                  <ReadinessView readiness={scenarioReadiness} />
                  <section className="panel result-panel">
                    <PlanView plan={scenarioPlan} />
                  </section>
                </>
              )}

              <ReviewPanel
                plan={plan}
                selectedScenario={selectedScenario}
                canApprove={canApprove}
                review={review}
                setReview={setReview}
                onSubmit={handleReview}
                loading={loading}
              />
            </>
          )}
        </section>
      </section>
    </main>
  );
}

function TextField({ label: fieldLabel, onChange, required = true, ...props }) {
  return (
    <label>
      {fieldLabel}
      <input required={required} onChange={(event) => onChange(event.target.value)} {...props} />
    </label>
  );
}
