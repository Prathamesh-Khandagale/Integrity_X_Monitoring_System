"""
Drift Detection Engine
Detects both gradual and sudden drift using multivariate analysis
"""

import numpy as np
from typing import Dict, List
from collections import deque


class DriftDetector:
    """Multivariate drift detection for gradual and sudden changes"""
    
    def __init__(self, window_size=50):
        self.window_size = window_size
        self.history = deque(maxlen=window_size)
        self.drift_scores = {}
        
    def detect(self, current_data: Dict[str, float], baseline: Dict) -> Dict:
        """
        Detect drift by comparing current data against baseline
        Returns drift information including type and severity
        """
        
        self.history.append(current_data.copy())
        
        if len(self.history) < 10:  # Need minimum history
            return {
                "drifting": False,
                "type": None,
                "severity": 0.0,
                "metrics_affected": [],
                "details": {}
            }
        
        drift_signals = {}
        metrics_affected = []
        
        # 1. Detect gradual drift for each metric
        for metric_name, value in current_data.items():
            if metric_name not in baseline or not isinstance(baseline[metric_name], dict):
                continue
            
            base_stats = baseline[metric_name]
            
            # Z-score deviation from baseline mean
            z_score = abs(value - base_stats["mean"]) / (base_stats["std"] + 1e-6)
            
            # Trend detection (slope of recent values)
            recent_values = [h[metric_name] for h in list(self.history)[-20:]]
            if len(recent_values) >= 10:
                trend = self._compute_trend(recent_values)
            else:
                trend = 0.0
            
            # Variance change
            current_variance = np.var([h[metric_name] for h in self.history])
            baseline_variance = base_stats["std"] ** 2
            variance_ratio = current_variance / (baseline_variance + 1e-6)
            
            drift_signals[metric_name] = {
                "z_score": z_score,
                "trend": trend,
                "variance_ratio": variance_ratio,
                "current_value": value,
                "baseline_mean": base_stats["mean"],
                "baseline_std": base_stats["std"]
            }
            
            # Flag if significantly drifted
            if z_score > 2.5 or abs(trend) > 0.3 or variance_ratio > 2.0:
                metrics_affected.append(metric_name)
        
        # 2. Detect sudden drift (shock events)
        sudden_drift_score = self._detect_sudden_drift(drift_signals)
        
        # 3. Detect gradual drift (sustained trend)
        gradual_drift_score = self._detect_gradual_drift(drift_signals)
        
        # 4. Correlation drift (relationship breakdown)
        correlation_drift_score = self._detect_correlation_drift(current_data, baseline)
        
        # 5. Overall drift assessment
        max_drift_score = max(sudden_drift_score, gradual_drift_score, correlation_drift_score)
        
        is_drifting = max_drift_score > 0.3  # Threshold
        
        # Determine drift type
        drift_type = None
        if is_drifting:
            if sudden_drift_score > gradual_drift_score:
                drift_type = "sudden"
            else:
                drift_type = "gradual"
        
        return {
            "drifting": is_drifting,
            "type": drift_type,
            "severity": round(max_drift_score, 3),
            "metrics_affected": metrics_affected,
            "details": {
                "sudden_score": round(sudden_drift_score, 3),
                "gradual_score": round(gradual_drift_score, 3),
                "correlation_score": round(correlation_drift_score, 3),
                "signals": {
                    k: {
                        "z_score": round(v["z_score"], 2),
                        "trend": round(v["trend"], 3)
                    }
                    for k, v in drift_signals.items()
                }
            }
        }
    
    def _compute_trend(self, values: List[float]) -> float:
        """Compute linear trend slope"""
        n = len(values)
        if n < 2:
            return 0.0
        
        x = np.arange(n)
        y = np.array(values)
        
        # Normalize y to make slope comparable across metrics
        y_normalized = (y - np.mean(y)) / (np.std(y) + 1e-6)
        
        coeffs = np.polyfit(x, y_normalized, 1)
        return coeffs[0]  # Slope
    
    def _detect_sudden_drift(self, drift_signals: Dict) -> float:
        """Detect sudden/shock drift events"""
        
        max_sudden_score = 0.0
        
        for metric_name, signals in drift_signals.items():
            z = signals["z_score"]
            
            # High z-score indicates sudden deviation
            if z > 3.0:
                sudden_score = min((z - 3.0) / 3.0, 1.0)  # Normalize
                max_sudden_score = max(max_sudden_score, sudden_score)
        
        return max_sudden_score
    
    def _detect_gradual_drift(self, drift_signals: Dict) -> float:
        """Detect gradual/sustained drift"""
        
        trend_scores = []
        
        for metric_name, signals in drift_signals.items():
            trend = abs(signals["trend"])
            variance_ratio = signals["variance_ratio"]
            
            # Sustained trend indicates gradual drift
            trend_score = min(trend / 0.5, 1.0)  # Normalize
            
            # Increasing variance also indicates drift
            variance_score = min(max(variance_ratio - 1.0, 0) / 2.0, 1.0)
            
            combined = max(trend_score, variance_score)
            trend_scores.append(combined)
        
        return max(trend_scores) if trend_scores else 0.0
    
    def _detect_correlation_drift(self, current_data: Dict, baseline: Dict) -> float:
        """Detect breakdown in metric correlations"""
        
        if "correlations" not in baseline or len(self.history) < 30:
            return 0.0
        
        # Compute current correlations from recent history
        metric_names = list(current_data.keys())
        recent_data = list(self.history)[-30:]
        
        current_correlations = {}
        for i, name1 in enumerate(metric_names):
            for j, name2 in enumerate(metric_names):
                if i < j:
                    values1 = [d[name1] for d in recent_data]
                    values2 = [d[name2] for d in recent_data]
                    
                    if np.std(values1) > 0 and np.std(values2) > 0:
                        corr = np.corrcoef(values1, values2)[0, 1]
                        key = f"{name1}_vs_{name2}"
                        current_correlations[key] = corr
        
        # Compare with baseline correlations
        correlation_diffs = []
        for key, baseline_corr in baseline["correlations"].items():
            if key in current_correlations:
                diff = abs(current_correlations[key] - baseline_corr)
                correlation_diffs.append(diff)
        
        if correlation_diffs:
            avg_diff = np.mean(correlation_diffs)
            return min(avg_diff / 0.5, 1.0)  # Normalize
        
        return 0.0
