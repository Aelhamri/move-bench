# -*- coding: utf-8 -*-
"""
MoveTrajectoryItem
==================
Custom QgsMapCanvasItem that animates trajectory features by drawing positions
directly via QPainter on the canvas, bypassing the QgsMapRendererJob pipeline.

This implements the "fast preview" alternative mode for MOVE (additive to the
standard QgsVectorLayer + symbol expression path).

Architectural rationale
-----------------------
QGIS's standard rendering pipeline (canvas.refresh() -> QgsMapRendererJob ->
re-composite all layers) has a fixed per-refresh cost of ~600+ ms regardless
of feature count. This is acknowledged at the QGIS-core level by QEP 72:
https://github.com/qgis/QGIS-Enhancement-Proposals/issues/72 — which proposes
exactly this kind of canvas-item bypass for fast-refresh use cases.

For MOVE animations on large feature counts (>5k features), this fixed cost
caps playback at ~1.5 FPS regardless of how cheap the per-feature rendering
is. By driving the animation through a custom QgsMapCanvasItem that subscribes
directly to QgsMapCanvas.temporalRangeChanged and paints with QPainter, MOVE
can reach 30-100+ FPS on the same datasets.

Trade-offs (vs standard QgsVectorLayer mode)
--------------------------------------------
The fast preview mode loses these QGIS integrations:
  - No QgsVectorLayer in layer tree (no visibility toggle, no opacity,
    no save/restore in .qgz)
  - No identify tool, no selection, no attribute table
  - No print composer / layout export (canvas items don't render into
    QgsLayoutItemMap)
  - No on-the-fly CRS reprojection (uses canvas dest CRS; data must
    already be in matching CRS for correct positioning)
  - No interop with other plugins (Trajectools / MovingPandas / qgis2web
    iterate QgsProject layers and won't see canvas items)

The standard MOVE mode (default) remains the right choice for analysis,
styling, querying. Use fast preview when you want fluid animation playback
on large datasets.
"""

import numpy as np
import psycopg

from qgis.PyQt.QtCore import Qt, QRectF
from qgis.PyQt.QtGui import QBrush, QColor, QPainter, QPen
from qgis.gui import QgsMapCanvasItem
from qgis.core import QgsPointXY


# Default rendering style (could be made configurable in a future iteration)
DEFAULT_POINT_COLOR = QColor(255, 60, 60, 200)   # semi-opaque red
DEFAULT_POINT_RADIUS_PX = 3


class MoveTrajectoryItem(QgsMapCanvasItem):
    """A QgsMapCanvasItem that animates trajectory positions by direct paint.

    Loads all trajectories from a Postgres materialized view (the same one MOVE
    creates for the standard mode) into local NumPy arrays. On each temporal
    range update from the canvas, paints the interpolated current position of
    every active trip directly via QPainter, without triggering a map render
    job.

    Lifecycle:
      __init__   : load matview rows into self._trips (NumPy arrays)
      attach_to_canvas_signals : connect QgsMapCanvas.temporalRangeChanged
      detach_from_canvas_signals + cleanup : disconnect + remove from scene
    """

    def __init__(self, canvas, view_name, db,
                 point_color=None, point_radius_px=None):
        """
        :param canvas: the QgsMapCanvas this item lives on
        :param view_name: name of the Postgres materialized view created by
                          MoveQuery.create_temporal_view (must contain columns
                          id, geom (LineStringM), start_t, end_t)
        :param db:        dict with host/port/database/username/password
        :param point_color: QColor for the moving point marker
        :param point_radius_px: marker radius in pixels
        """
        super(MoveTrajectoryItem, self).__init__(canvas)
        # NB: QgsMapCanvasItem.canvas() is not exposed in PyQGIS (protected
        # C++ method), so we keep the reference ourselves.
        self._canvas = canvas
        self._view_name = view_name
        self._db = db
        self._point_color = point_color or DEFAULT_POINT_COLOR
        self._point_radius = point_radius_px or DEFAULT_POINT_RADIUS_PX

        self._trips = []          # list of dicts: {x, y, m, s_epoch, e_epoch}
        self._current_epoch = None
        self._signal_connected = False

        self._load_trips()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def _load_trips(self):
        """Fetch all features from the matview and parse LineStringM vertices
        into per-trip NumPy arrays (x, y, m).

        Format of ST_AsText for LineStringM is:
            "LINESTRING M (x1 y1 m1, x2 y2 m2, ...)"
        We use ST_AsText rather than ST_AsGeoJSON because GeoJSON does not
        preserve M coordinates (specification limitation).
        """
        sql = (
            "SELECT id, ST_AsText(geom), "
            "EXTRACT(EPOCH FROM start_t), EXTRACT(EPOCH FROM end_t) "
            "FROM \"{view}\" ORDER BY id"
        ).format(view=self._view_name)

        with psycopg.connect(
                host=self._db['host'],
                port=self._db['port'],
                dbname=self._db['database'],
                user=self._db['username'],
                password=self._db['password']) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()

        for fid, wkt, s_epoch, e_epoch in rows:
            if s_epoch is None or e_epoch is None:
                continue
            if not wkt or '(' not in wkt:
                continue
            inner = wkt[wkt.index('(') + 1: wkt.rindex(')')]
            coords = []
            for triple in inner.split(','):
                parts = triple.strip().split()
                if len(parts) >= 3:
                    try:
                        coords.append([
                            float(parts[0]), float(parts[1]), float(parts[2])
                        ])
                    except ValueError:
                        pass
            if len(coords) < 2:
                continue
            arr = np.asarray(coords, dtype=np.float64)
            self._trips.append({
                'x':       arr[:, 0],
                'y':       arr[:, 1],
                'm':       arr[:, 2],
                's_epoch': float(s_epoch),
                'e_epoch': float(e_epoch),
            })

    # ------------------------------------------------------------------
    # Signal wiring (animation driver)
    # ------------------------------------------------------------------
    def attach_to_canvas_signals(self):
        """Connect to QgsMapCanvas.temporalRangeChanged so that the item
        repaints whenever the user advances the QGIS Temporal Controller."""
        if not self._signal_connected:
            self._canvas.temporalRangeChanged.connect(self.on_temporal_range)
            self._signal_connected = True
            # Initialize with the canvas's current temporal range, if any
            try:
                self.on_temporal_range(self._canvas.temporalRange())
            except Exception:
                pass

    def detach_from_canvas_signals(self):
        if self._signal_connected:
            try:
                self._canvas.temporalRangeChanged.disconnect(
                    self.on_temporal_range)
            except (TypeError, RuntimeError):
                # Already disconnected, or canvas has been deleted
                pass
            self._signal_connected = False

    def on_temporal_range(self, *args):
        """Slot: update current epoch from the canvas's temporal range and
        request a repaint.

        We accept *args because QgsMapCanvas.temporalRangeChanged signal
        emission does not always pass the QgsDateTimeRange argument across
        QGIS versions — we always read canvas.temporalRange() directly to
        be robust."""
        qgs_dt_range = self._canvas.temporalRange()
        if qgs_dt_range is None:
            return
        end = qgs_dt_range.end()
        if end is None or not end.isValid():
            return
        self._current_epoch = end.toMSecsSinceEpoch() / 1000.0
        self.update()

    # ------------------------------------------------------------------
    # QGraphicsItem overrides
    # ------------------------------------------------------------------
    def boundingRect(self):
        s = self._canvas.size()
        return QRectF(0, 0, s.width(), s.height())

    def paint(self, painter, option, widget):
        if self._current_epoch is None:
            return

        epoch = self._current_epoch
        m2p = self._canvas.getCoordinateTransform()

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QBrush(self._point_color))
        painter.setPen(QPen(Qt.NoPen))

        r = self._point_radius
        d = r * 2

        for trip in self._trips:
            if epoch < trip['s_epoch'] or epoch > trip['e_epoch']:
                continue
            m = trip['m']
            # Find segment containing epoch via NumPy binary search
            i = int(np.searchsorted(m, epoch, side='right')) - 1
            if i < 0 or i >= len(m) - 1:
                continue
            m0 = m[i]
            m1 = m[i + 1]
            if m1 == m0:
                continue
            frac = (epoch - m0) / (m1 - m0)
            x = trip['x'][i] + frac * (trip['x'][i + 1] - trip['x'][i])
            y = trip['y'][i] + frac * (trip['y'][i + 1] - trip['y'][i])
            pt = m2p.transform(QgsPointXY(x, y))
            painter.drawEllipse(int(pt.x()) - r, int(pt.y()) - r, d, d)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def cleanup(self):
        """Detach signal, remove from scene, drop trip data."""
        self.detach_from_canvas_signals()
        try:
            scene = self._canvas.scene()
            if self in scene.items():
                scene.removeItem(self)
        except Exception:
            pass
        self._trips = []

    # ------------------------------------------------------------------
    # Introspection helpers (useful for tests/debug)
    # ------------------------------------------------------------------
    def trip_count(self):
        return len(self._trips)

    def view_name(self):
        return self._view_name
