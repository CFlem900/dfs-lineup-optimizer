/**
 * Frontend mirror of the backend sport registry.
 *
 * Per-sport static metadata used for slot rendering and position filters
 * BEFORE any lineup has been generated. After generation, prefer
 * ``lineup.roster_slots`` from the API response since that's the
 * authoritative shape for the actual draft group.
 *
 * Adding a sport: add an entry to ``SPORT_SHAPES`` keyed by sport code,
 * mirroring the values in ``backend/app/sports/<code>.py``. Keep this
 * file in sync with the backend registry — the
 * ``test_sport_config.py`` legacy-parity tests prevent backend drift,
 * but there's no automatic check that this file matches.
 *
 * Stacking controls (Prompt 5.2):
 *   The ``stackingControls`` property describes which dropdowns the
 *   StackingPanel should render for the sport. The panel reads the
 *   shape, builds the appropriate inputs, and POSTs the resulting
 *   ``primary_stack_size`` / ``secondary_stack_size`` /
 *   ``require_bring_back`` fields to ``/api/generate-lineups``. The
 *   backend Pydantic model + sport-aware ILP helpers (Prompt 5.1)
 *   consume those fields and override the ``SportConfig.stack_rules``
 *   defaults. NBA / CBB are ``null`` because they use the legacy game-
 *   stack flow driven by the existing ``enable_stacking`` boolean.
 */

export const SPORT_SHAPES = {
  nba: {
    label: 'NBA',
    dkRosterSlots: ['PG', 'SG', 'SF', 'PF', 'C', 'G', 'F', 'UTIL'],
    positionFilters: ['ALL', 'PG', 'SG', 'SF', 'PF', 'C'],
    salaryCapDk: 50000,
    // NBA uses the legacy game-stack pathway driven by the
    // ``enable_stacking`` boolean alone; the StackingPanel renders no
    // sport-specific dropdowns when this is null.
    stackingControls: null,
  },
  cbb: {
    label: 'NCAA',
    dkRosterSlots: ['G', 'G', 'G', 'F', 'F', 'F', 'UTIL', 'UTIL'],
    positionFilters: ['ALL', 'G', 'F', 'C'],
    salaryCapDk: 50000,
    // Same legacy pathway as NBA — no sport-specific stacking controls.
    stackingControls: null,
  },
  nfl: {
    label: 'NFL',
    dkRosterSlots: ['QB', 'RB', 'RB', 'WR', 'WR', 'WR', 'TE', 'FLEX', 'DST'],
    positionFilters: ['ALL', 'QB', 'RB', 'WR', 'TE', 'DST'],
    salaryCapDk: 50000,
    // QB-stack control + bring-back toggle. ``primaryOptions`` map to
    // ``primary_stack_size`` (qb_min_pass_catchers) on the request.
    // ``hasBringBack`` tells the panel to render the bring-back checkbox.
    stackingControls: {
      type: 'nfl_style',
      primaryLabel: 'QB + Receivers',
      primaryOptions: [1, 2, 3], // 1, 2, or 3 same-team WR/TE behind QB
      hasBringBack: true,
    },
  },
  mlb: {
    label: 'MLB',
    dkRosterSlots: ['P', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF', 'OF', 'OF'],
    positionFilters: ['ALL', 'P', 'C', '1B', '2B', '3B', 'SS', 'OF'],
    salaryCapDk: 50000,
    // Two dropdowns — primary 5/4/3 hitter stack and secondary 4/3/2/0.
    // 0 in ``secondaryOptions`` means "no secondary stack" (sent as
    // ``secondary_stack_size: 0`` to disable the soft bonus). The
    // backend model_validator rejects ``primary + secondary > 8`` so
    // the panel can also pre-validate by the same rule.
    stackingControls: {
      type: 'mlb_style',
      primaryLabel: 'Primary Stack',
      primaryOptions: [5, 4, 3],
      secondaryLabel: 'Secondary Stack',
      secondaryOptions: [4, 3, 2, 0],
    },
  },
}

/** Default for unknown sports — fall back to NBA so the UI doesn't crash. */
const DEFAULT_SHAPE = SPORT_SHAPES.nba

/**
 * Read the per-sport shape, falling back to NBA on unknown sport rather
 * than throwing — matches the backend's defensive ``_resolve_sport_service``.
 *
 * @param {string} sport
 * @returns {{label:string,dkRosterSlots:string[],positionFilters:string[],salaryCapDk:number,stackingControls:object|null}}
 */
export function getSportShape(sport) {
  if (!sport) return DEFAULT_SHAPE
  return SPORT_SHAPES[sport.toLowerCase()] ?? DEFAULT_SHAPE
}

/** Roster slot labels for a sport (DK Classic). */
export function getDkRosterSlots(sport) {
  return getSportShape(sport).dkRosterSlots
}

/** Position-filter chips for the player pool table. */
export function getPositionFilters(sport) {
  return getSportShape(sport).positionFilters
}

/**
 * Stacking-controls schema for the sport, or ``null`` when the sport
 * has no per-request stacking dropdowns (NBA / CBB use only the
 * ``enable_stacking`` boolean).
 *
 * Consumers (e.g. StackingPanel) should treat ``null`` as "render
 * nothing" rather than "render defaults" — the boolean toggle lives
 * elsewhere in the form.
 *
 * @param {string} sport
 * @returns {null | {type:'nfl_style',primaryLabel:string,primaryOptions:number[],hasBringBack:boolean} | {type:'mlb_style',primaryLabel:string,primaryOptions:number[],secondaryLabel:string,secondaryOptions:number[]}}
 */
export function getStackingControls(sport) {
  return getSportShape(sport).stackingControls ?? null
}
