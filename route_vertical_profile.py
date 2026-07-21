"""Render the route side-profile SVG, including altitude bands for visible hazards."""

from __future__ import annotations

import html
import math

from weather_core import RouteVerticalProfile, RouteVerticalProfileHazardSpan

# Known types get first-class styling and legend order; spans with any other
# hazard_type still render with the generic style rather than disappearing.
KNOWN_HAZARD_TYPES = (
    "icing",
    "turbulence",
    "convective",
    "ifr",
    "mountain_obscuration",
    "surface_wind",
    "llws",
)


# Styling is kept in code so the Streamlit view and tests share one rendering path.
def _hazard_style(span: RouteVerticalProfileHazardSpan) -> tuple[str, str, str]:
    """Return stable fill, stroke, and label text for one rendered hazard span."""

    styles = {
        "convective": ("#c0267c", "#8f175c", "Convective"),
        "turbulence": ("#d97706", "#a45309", "Turbulence"),
        "icing": ("#0f766e", "#155e75", "Icing"),
        "ifr": ("#7c3aed", "#5b21b6", "IFR"),
        "mountain_obscuration": ("#64748b", "#334155", "Mtn Obsc"),
        "surface_wind": ("#dc2626", "#991b1b", "Sfc Wind"),
        "llws": ("#ea580c", "#9a3412", "LLWS"),
    }
    fill, stroke, label = styles.get(span.hazard_type, ("#475569", "#1f2937", span.hazard_type.title()))
    return fill, stroke, label


def _fitted_hazard_label_markup(
    *,
    label: str,
    x: float,
    y: float,
    width: float,
    height: float,
    color: str,
    occupied_rectangles: list[tuple[float, float, float, float]],
    minimum_y: float,
    maximum_y: float,
) -> str:
    """Place a fitted hazard name from the band's upper-left without text collisions."""

    uppercase_label = label.upper()
    horizontal_inset = min(6.0, max(width / 4.0, 2.0))
    vertical_inset = min(5.0, max(height / 4.0, 2.0))
    available_width = max(width - (horizontal_inset * 2.0), 4.0)
    available_height = max(height - (vertical_inset * 2.0), 6.0)
    fitted_font_size = min(
        12.0,
        available_height,
        available_width / (max(len(uppercase_label), 1) * 0.58),
    )
    minimum_font_size = min(7.0, fitted_font_size)
    font_sizes = []
    current_font_size = fitted_font_size
    while current_font_size >= minimum_font_size:
        font_sizes.append(current_font_size)
        current_font_size -= 1.0
    if not font_sizes:
        font_sizes = [minimum_font_size]

    label_x = x + horizontal_inset
    for font_size in font_sizes:
        estimated_width = _estimate_text_width(uppercase_label, font_size)
        rendered_width = min(estimated_width, available_width)
        lane_step = font_size + 5.0
        first_baseline = y + vertical_inset + font_size
        last_baseline = y + height - vertical_inset
        candidate_y = first_baseline
        while candidate_y <= last_baseline + 0.01:
            bbox = (
                label_x,
                candidate_y - font_size - 2.0,
                label_x + rendered_width,
                candidate_y + 2.0,
            )
            if not any(
                _rectangles_overlap(bbox, existing, padding=3.0)
                for existing in occupied_rectangles
            ):
                occupied_rectangles.append(bbox)
                length_adjust = (
                    f' textLength="{available_width:.1f}" lengthAdjust="spacingAndGlyphs"'
                    if estimated_width > available_width
                    else ""
                )
                return (
                    f'<text data-hazard-label="{html.escape(uppercase_label)}" '
                    f'x="{label_x:.2f}" y="{candidate_y:.2f}" text-anchor="start" '
                    f'font-size="{font_size:.1f}" font-weight="700" fill="{color}"{length_adjust}>'
                    f'{html.escape(uppercase_label)}</text>'
                )
            candidate_y += lane_step

    # A very thin or completely overlaid band may have no remaining interior
    # lane. Keep its label tied to the upper-left x coordinate and search the
    # chart vertically instead of dropping the region name or overlapping text.
    fallback_font_size = minimum_font_size
    estimated_width = _estimate_text_width(uppercase_label, fallback_font_size)
    rendered_width = min(estimated_width, available_width)
    lane_step = fallback_font_size + 5.0
    first_baseline = min(
        max(y + vertical_inset + fallback_font_size, minimum_y + fallback_font_size + 2.0),
        maximum_y - 2.0,
    )
    max_lane_count = max(int((maximum_y - minimum_y) / max(lane_step, 1.0)) + 2, 2)
    for lane_index in range(max_lane_count):
        offsets = (0.0,) if lane_index == 0 else (lane_index * lane_step, -lane_index * lane_step)
        for offset in offsets:
            candidate_y = first_baseline + offset
            if candidate_y - fallback_font_size - 2.0 < minimum_y or candidate_y + 2.0 > maximum_y:
                continue
            bbox = (
                label_x,
                candidate_y - fallback_font_size - 2.0,
                label_x + rendered_width,
                candidate_y + 2.0,
            )
            if any(
                _rectangles_overlap(bbox, existing, padding=3.0)
                for existing in occupied_rectangles
            ):
                continue
            occupied_rectangles.append(bbox)
            length_adjust = (
                f' textLength="{available_width:.1f}" lengthAdjust="spacingAndGlyphs"'
                if estimated_width > available_width
                else ""
            )
            return (
                f'<text data-hazard-label="{html.escape(uppercase_label)}" '
                f'x="{label_x:.2f}" y="{candidate_y:.2f}" text-anchor="start" '
                f'font-size="{fallback_font_size:.1f}" font-weight="700" fill="{color}"{length_adjust}>'
                f'{html.escape(uppercase_label)}</text>'
            )

    # This is reachable only when the entire plot column is saturated with
    # labels. Preserve the name in accessible SVG metadata without adding
    # unreadable glyphs to an already exhausted chart.
    return ""


def _display_ceiling_ft(vertical_profile: RouteVerticalProfile) -> int:
    """Pick a chart ceiling high enough for route altitude, hazards, and field elevations."""

    max_hazard_top_ft = max(
        (span.top_ft for span in vertical_profile.hazard_spans),
        default=vertical_profile.cruise_altitude_ft,
    )
    target_ft = max(
        vertical_profile.cruise_altitude_ft + 4000,
        max_hazard_top_ft + 2000,
        max(vertical_profile.departure_elevation_ft, vertical_profile.destination_elevation_ft) + 4000,
        12000,
    )
    step_ft = 12000 if target_ft >= 36000 else 6000
    return int(math.ceil(target_ft / step_ft) * step_ft)


def _estimate_text_width(text: str, font_size: float) -> float:
    """Approximate SVG text width so labels can avoid overlap without browser layout access."""

    return max(len(text), 1) * font_size * 0.58


def _text_bbox(
    x: float,
    y: float,
    text: str,
    *,
    font_size: float,
    anchor: str,
) -> tuple[float, float, float, float]:
    """Approximate an SVG text bounding box for collision checks."""

    width = _estimate_text_width(text, font_size)
    height = font_size + 4.0
    if anchor == "middle":
        left = x - (width / 2.0)
    elif anchor == "end":
        left = x - width
    else:
        left = x
    return (left, y - height, left + width, y + 4.0)


def _rectangles_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    padding: float = 0.0,
) -> bool:
    """Return whether two approximate SVG label rectangles overlap."""

    return not (
        (left[2] + padding) < right[0]
        or (right[2] + padding) < left[0]
        or (left[3] + padding) < right[1]
        or (right[3] + padding) < left[1]
    )


def _try_place_text(
    *,
    text: str,
    candidates: list[tuple[float, float, str]],
    font_size: float,
    occupied_rectangles: list[tuple[float, float, float, float]],
    fill: str,
    font_weight: str = "700",
) -> str:
    """Place text in the first free slot so labels stay readable on dense profiles."""

    for x, y, anchor in candidates:
        bbox = _text_bbox(x, y, text, font_size=font_size, anchor=anchor)
        if any(_rectangles_overlap(bbox, existing, padding=4.0) for existing in occupied_rectangles):
            continue
        occupied_rectangles.append(bbox)
        return (
            f"<text x=\"{x}\" y=\"{y}\" text-anchor=\"{anchor}\" font-size=\"{font_size:.0f}\" "
            f"font-weight=\"{font_weight}\" fill=\"{fill}\">{html.escape(text)}</text>"
        )
    return ""


def build_route_vertical_profile_svg(
    vertical_profile: RouteVerticalProfile,
    *,
    departure_label: str,
    destination_label: str,
    selected_flight_level_label: str,
    visible_hazard_types: set[str] | None = None,
    width: int = 960,
    height: int = 408,
) -> str:
    """Render the selected flight level as an SVG profile with optional hazard filtering."""

    left_pad = 86.0
    right_pad = 28.0
    top_pad = 42.0
    bottom_pad = 104.0
    plot_width = max(float(width) - left_pad - right_pad, 10.0)
    plot_height = max(float(height) - top_pad - bottom_pad, 10.0)
    mission_distance_nm = max(vertical_profile.mission_distance_nm, 1.0)
    display_ceiling_ft = _display_ceiling_ft(vertical_profile)
    # Render every type present in the data, not just the known set, so a novel
    # hazard_type from a feed change cannot silently disappear from the profile.
    hazard_types_in_order = list(KNOWN_HAZARD_TYPES) + sorted(
        {span.hazard_type.lower() for span in vertical_profile.hazard_spans} - set(KNOWN_HAZARD_TYPES)
    )
    active_hazard_types = (
        {value.lower() for value in visible_hazard_types}
        if visible_hazard_types is not None
        else set(hazard_types_in_order)
    )
    visible_hazard_spans = [
        span for span in vertical_profile.hazard_spans if span.hazard_type.lower() in active_hazard_types
    ]

    def plot_x(distance_nm: float) -> float:
        # Feed geometry can extend slightly outside the flown route. Clamp it so
        # off-route advisories cannot draw beyond the SVG plotting frame.
        clamped_distance_nm = min(max(float(distance_nm), 0.0), mission_distance_nm)
        return round(left_pad + ((clamped_distance_nm / mission_distance_nm) * plot_width), 2)

    def plot_y(altitude_ft: float) -> float:
        clamped_altitude_ft = min(max(float(altitude_ft), 0.0), float(display_ceiling_ft))
        return round(top_pad + ((display_ceiling_ft - clamped_altitude_ft) / display_ceiling_ft) * plot_height, 2)

    grid_step_ft = 12000 if display_ceiling_ft >= 36000 else 6000
    grid_lines = []
    for altitude_ft in range(0, display_ceiling_ft + grid_step_ft, grid_step_ft):
        y = plot_y(altitude_ft)
        grid_lines.append(
            f"<line x1=\"{left_pad}\" y1=\"{y}\" x2=\"{width - right_pad}\" y2=\"{y}\" "
            "stroke=\"#d6d1c8\" stroke-width=\"1\" stroke-dasharray=\"5 5\"/>"
        )
        grid_lines.append(
            f"<text x=\"{left_pad - 10}\" y=\"{y + 5}\" text-anchor=\"end\" font-size=\"13\" "
            "font-weight=\"700\" fill=\"#6a6962\">"
            f"{altitude_ft:,}</text>"
        )

    cruise_y = plot_y(vertical_profile.cruise_altitude_ft)
    cruise_line = (
        f"<line x1=\"{left_pad}\" y1=\"{cruise_y}\" x2=\"{width - right_pad}\" y2=\"{cruise_y}\" "
        "stroke=\"#a81d73\" stroke-width=\"1.5\" stroke-dasharray=\"8 6\" opacity=\"0.82\"/>"
        f"<text x=\"{width - right_pad - 8}\" y=\"{cruise_y - 8}\" text-anchor=\"end\" font-size=\"13\" "
        "font-weight=\"800\" fill=\"#8b155f\">"
        f"{html.escape(selected_flight_level_label)}</text>"
    )

    hazard_fills: dict[str, list[str]] = {}
    hazard_annotations: dict[str, list[str]] = {}
    occupied_band_labels: list[tuple[float, float, float, float]] = []
    for span in visible_hazard_spans:
        fill_color, stroke_color, label = _hazard_style(span)
        hazard_type = span.hazard_type.lower()
        x = plot_x(span.start_distance_nm)
        raw_width_px = max(plot_x(span.end_distance_nm) - x, 0.0)
        width_px = max(raw_width_px, 8.0)
        y_top = plot_y(span.top_ft)
        y_bottom = plot_y(span.base_ft)
        raw_height_px = max(y_bottom - y_top, 0.0)
        rect_height = max(raw_height_px, 8.0)
        label_x = min(x + width_px - 6.0, width - right_pad - 6.0)
        visual_minimum_applied = raw_width_px < 8.0 or raw_height_px < 8.0
        title_text = (
            f"{label} | {span.base_ft:,}-{span.top_ft:,} ft | "
            f"{span.start_distance_nm:.0f}-{span.end_distance_nm:.0f} NM | {span.source}"
            f"{' | enlarged to minimum visible size' if visual_minimum_applied else ''}"
        )
        minimum_cue = (
            f'<circle cx="{x + width_px - 3.0}" cy="{y_top + 3.0}" r="2.5" fill="{stroke_color}">'
            "<title>Minimum visible size marker</title></circle>"
            if visual_minimum_applied
            else ""
        )
        tag_markup = _fitted_hazard_label_markup(
            label=label,
            x=x,
            y=y_top,
            width=width_px,
            height=rect_height,
            color=stroke_color,
            occupied_rectangles=occupied_band_labels,
            minimum_y=top_pad,
            maximum_y=top_pad + plot_height,
        )
        # Try above the band first, then inside it, so crowded overlays still show useful labels.
        top_label_markup = _try_place_text(
            text=f"{span.top_ft:,}",
            candidates=[
                (label_x, max(y_top - 6.0, top_pad + 12.0), "end"),
                (label_x, min(y_top + 16.0, y_bottom - 8.0), "end"),
            ],
            font_size=12.0,
            occupied_rectangles=occupied_band_labels,
            fill=stroke_color,
        )
        base_label_markup = _try_place_text(
            text=f"{span.base_ft:,}",
            candidates=[
                (label_x, min(y_bottom + 14.0, top_pad + plot_height + 18.0), "end"),
                (label_x, max(y_bottom - 6.0, y_top + 14.0), "end"),
            ],
            font_size=12.0,
            occupied_rectangles=occupied_band_labels,
            fill=stroke_color,
        )
        hazard_fills.setdefault(hazard_type, []).append(
            "<g>"
            f"<title>{html.escape(title_text)}</title>"
            f"<rect x=\"{x}\" y=\"{y_top}\" width=\"{width_px}\" height=\"{rect_height}\" "
            f"fill=\"{fill_color}\" rx=\"6\"/>"
            "</g>"
        )
        hazard_annotations.setdefault(hazard_type, []).append(
            "<g>"
            f"<rect x=\"{x}\" y=\"{y_top}\" width=\"{width_px}\" height=\"{rect_height}\" "
            f"fill=\"none\" stroke=\"{stroke_color}\" stroke-width=\"2\" stroke-dasharray=\"6 4\" rx=\"6\"/>"
            f"{minimum_cue}"
            f"{tag_markup}"
            f"{top_label_markup}"
            f"{base_label_markup}"
            "</g>"
        )

    hazard_layers = []
    for hazard_type in hazard_types_in_order:
        fills = hazard_fills.get(hazard_type, [])
        annotations = hazard_annotations.get(hazard_type, [])
        if not fills and not annotations:
            continue
        hazard_layers.append(
            f'<g class="hazard-layer" data-hazard-type="{hazard_type}">'
            f'<g class="hazard-fill-layer" opacity="0.42">{"".join(fills)}</g>'
            f'{"".join(annotations)}'
            "</g>"
        )

    path_points = " ".join(
        f"{plot_x(point.distance_nm)},{plot_y(point.altitude_ft)}"
        for point in vertical_profile.path_points
    )
    flight_path = (
        f"<polyline points=\"{path_points}\" fill=\"none\" stroke=\"#d2911f\" stroke-opacity=\"0.26\" "
        "stroke-width=\"10\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>"
        f"<polyline points=\"{path_points}\" fill=\"none\" stroke=\"#a81d73\" "
        "stroke-width=\"4\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>"
    )

    departure_x = plot_x(0.0)
    destination_x = plot_x(vertical_profile.mission_distance_nm)
    ground_y = plot_y(0.0)
    departure_airport_y = plot_y(vertical_profile.departure_elevation_ft)
    destination_airport_y = plot_y(vertical_profile.destination_elevation_ft)
    # Reserve the airport label area first so intermediate waypoint tags can dodge it cleanly.
    waypoint_guides: list[str] = []
    occupied_waypoint_labels: list[tuple[float, float, float, float]] = [
        _text_bbox(departure_x, height - 68, departure_label, font_size=18.0, anchor="middle"),
        _text_bbox(destination_x, height - 68, destination_label, font_size=18.0, anchor="middle"),
    ]
    for waypoint_marker in vertical_profile.waypoint_markers:
        waypoint_x = plot_x(waypoint_marker.distance_nm)
        is_fuel_stop = bool(getattr(waypoint_marker, "is_fuel_stop", False))
        marker_fill = "#0f5f67" if is_fuel_stop else "#cf8c2b"
        marker_radius = 6.2 if is_fuel_stop else 4.5
        # The guide line keeps the label visually tied to its cumulative route distance when the
        # text itself needs to shift up or down to avoid overlapping the airport labels.
        waypoint_guides.append(
            f"<line x1=\"{waypoint_x}\" y1=\"{top_pad}\" x2=\"{waypoint_x}\" y2=\"{ground_y}\" "
            "stroke=\"#c7b596\" stroke-width=\"1.1\" stroke-dasharray=\"4 6\" opacity=\"0.62\"/>"
        )
        waypoint_guides.append(
            f"<circle cx=\"{waypoint_x}\" cy=\"{ground_y}\" r=\"{marker_radius}\" fill=\"{marker_fill}\" "
            "stroke=\"#fbfaf6\" stroke-width=\"2\"/>"
        )
        waypoint_label = _try_place_text(
            text=waypoint_marker.identifier,
            candidates=[
                (waypoint_x, height - 84.0, "middle"),
                (waypoint_x, height - 96.0, "middle"),
                (waypoint_x, height - 56.0, "middle"),
            ],
            font_size=12.0,
            font_weight="800",
            occupied_rectangles=occupied_waypoint_labels,
            fill="#7f5e26",
        )
        if waypoint_label:
            waypoint_guides.append(waypoint_label)

    header_labels = []
    occupied_header: list[tuple[float, float, float, float]] = []
    header_labels.append(
        _try_place_text(
            text="ROUTE PROFILE",
            candidates=[(left_pad, 20.0, "start")],
            font_size=12.0,
            font_weight="800",
            occupied_rectangles=occupied_header,
            fill="#6a6962",
        )
    )
    for label_text, distance_nm in (
        (f"{int(round(vertical_profile.mission_distance_nm / 2.0))} NM", vertical_profile.mission_distance_nm / 2.0),
        (f"{int(round(vertical_profile.mission_distance_nm))} NM", vertical_profile.mission_distance_nm),
    ):
        x = plot_x(distance_nm)
        header_labels.append(
            _try_place_text(
                text=label_text,
                candidates=[(x, 20.0, "middle")],
                font_size=12.0,
                occupied_rectangles=occupied_header,
                fill="#6a6962",
            )
        )

    legend_specs = [
        ("icing", "Icing", "#0f766e"),
        ("turbulence", "Turbulence", "#d97706"),
        ("convective", "Convective", "#c0267c"),
        ("ifr", "IFR", "#7c3aed"),
        ("mountain_obscuration", "Mtn Obsc", "#64748b"),
        ("surface_wind", "Sfc Wind", "#dc2626"),
        ("llws", "LLWS", "#ea580c"),
    ]
    active_legend_specs = [spec for spec in legend_specs if spec[0] in active_hazard_types]
    legend_spacing = 104.0 if len(active_legend_specs) > 4 else 128.0
    legend_width = max((len(active_legend_specs) * legend_spacing) - 18.0, 0.0)
    legend_start_x = max(((width - legend_width) / 2.0), left_pad + 36.0)
    legend_items = []
    for idx, (key, label, color) in enumerate(active_legend_specs):
        x = legend_start_x + (idx * legend_spacing)
        legend_items.append(
            f'<g class="hazard-legend-toggle" data-hazard-toggle="{key}" role="button" tabindex="0" '
            f'aria-pressed="true" aria-label="Toggle {html.escape(label)} hazard bands">'
            f'<rect class="hazard-legend-hitbox" x="{x - 6.0}" y="{height - 42}" width="{legend_spacing - 4.0}" '
            'height="28" rx="8" fill="transparent"/>'
            f"<rect x=\"{x}\" y=\"{height - 34}\" width=\"18\" height=\"12\" rx=\"3\" fill=\"{color}\" fill-opacity=\"0.42\" "
            f"stroke=\"{color}\" stroke-width=\"2\"/>"
            f"<text x=\"{x + 26}\" y=\"{height - 23}\" font-size=\"12\" font-weight=\"700\" fill=\"#585850\">"
            f"{label}</text></g>"
        )
    footer_note = (
        f"<text x=\"{width / 2.0}\" y=\"{height - 52}\" text-anchor=\"middle\" font-size=\"12\" "
        "font-weight=\"700\" fill=\"#6a6962\">Band labels show base/top feet MSL</text>"
    )

    empty_message = ""
    if not visible_hazard_spans:
        empty_message = (
            f"<text x=\"{left_pad + (plot_width / 2.0)}\" y=\"{top_pad + 28}\" text-anchor=\"middle\" "
            "font-size=\"14\" font-weight=\"700\" fill=\"#6a6962\">No visible route hazard bands currently intersect the route timeline.</text>"
        )

    return (
        f"<svg viewBox=\"0 0 {width} {height}\" width=\"100%\" height=\"100%\" "
        "preserveAspectRatio=\"xMidYMid meet\" xmlns=\"http://www.w3.org/2000/svg\">"
        "<rect width=\"100%\" height=\"100%\" rx=\"28\" fill=\"#f7f3eb\"/>"
        f"<rect x=\"10\" y=\"10\" width=\"{width - 20}\" height=\"{height - 20}\" rx=\"24\" "
        "fill=\"#fbfaf6\" stroke=\"#d8d2c8\" stroke-width=\"1.2\"/>"
        f"{''.join(value for value in header_labels if value)}"
        f"{''.join(grid_lines)}"
        f"{cruise_line}"
        f"{''.join(hazard_layers)}"
        f"{flight_path}"
        f"{''.join(waypoint_guides)}"
        f"{empty_message}"
        f"<line x1=\"{left_pad}\" y1=\"{ground_y}\" x2=\"{width - right_pad}\" y2=\"{ground_y}\" stroke=\"#8f8576\" stroke-width=\"1.4\"/>"
        f"<circle cx=\"{departure_x}\" cy=\"{departure_airport_y}\" r=\"7\" fill=\"#d2911f\" stroke=\"#fbfaf6\" stroke-width=\"3\"/>"
        f"<circle cx=\"{destination_x}\" cy=\"{destination_airport_y}\" r=\"8\" fill=\"#f43f5e\" stroke=\"#fbfaf6\" stroke-width=\"3\"/>"
        f"<text x=\"{departure_x}\" y=\"{height - 68}\" text-anchor=\"middle\" font-size=\"18\" font-weight=\"900\" fill=\"#403c36\">"
        f"{html.escape(departure_label)}</text>"
        f"<text x=\"{destination_x}\" y=\"{height - 68}\" text-anchor=\"middle\" font-size=\"18\" font-weight=\"900\" fill=\"#403c36\">"
        f"{html.escape(destination_label)}</text>"
        f"<text x=\"{departure_x}\" y=\"{height - 48}\" text-anchor=\"middle\" font-size=\"12\" font-weight=\"700\" fill=\"#6a6962\">"
        f"{vertical_profile.departure_elevation_ft:,} ft</text>"
        f"<text x=\"{destination_x}\" y=\"{height - 48}\" text-anchor=\"middle\" font-size=\"12\" font-weight=\"700\" fill=\"#6a6962\">"
        f"{vertical_profile.destination_elevation_ft:,} ft</text>"
        f"{footer_note}"
        f"{''.join(legend_items)}"
        "</svg>"
    )


def build_interactive_route_vertical_profile_html(svg: str) -> str:
    """Wrap a profile SVG with local legend controls that never rerun Streamlit."""

    return (
        '<div id="route-profile-interactive" class="route-profile-interactive">'
        f"{svg}"
        "</div>"
        "<style>"
        "html,body{margin:0;padding:0;background:transparent;overflow:hidden;}"
        ".route-profile-interactive{width:100%;}"
        ".route-profile-interactive svg{display:block;width:100%;height:auto;}"
        ".hazard-legend-toggle{cursor:pointer;transition:opacity .16s ease;}"
        ".hazard-legend-toggle.is-hidden{opacity:.28;}"
        ".hazard-legend-toggle:focus-visible .hazard-legend-hitbox{stroke:#403c36;stroke-width:2;}"
        "@media (prefers-reduced-motion:reduce){.hazard-legend-toggle{transition:none;}}"
        "</style>"
        "<script>"
        "(()=>{"
        "const root=document.getElementById('route-profile-interactive');"
        "if(!root)return;"
        "const toggles=[...root.querySelectorAll('[data-hazard-toggle]')];"
        "const setVisible=(toggle,visible)=>{"
        "const key=toggle.dataset.hazardToggle;"
        "root.querySelectorAll('[data-hazard-type=\"'+key+'\"]').forEach(layer=>{"
        "layer.style.display=visible?'':'none';"
        "});"
        "toggle.setAttribute('aria-pressed',String(visible));"
        "toggle.classList.toggle('is-hidden',!visible);"
        "};"
        "const activate=toggle=>setVisible(toggle,toggle.getAttribute('aria-pressed')!=='true');"
        "toggles.forEach(toggle=>{"
        "toggle.addEventListener('click',()=>activate(toggle));"
        "toggle.addEventListener('keydown',event=>{"
        "if(event.key==='Enter'||event.key===' '){event.preventDefault();activate(toggle);}});"
        "});"
        "})();"
        "</script>"
    )
