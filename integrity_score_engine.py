"""
Enterprise Integrity Scoring Engine
Mathematical composite score with weighted factors
"""

import numpy as np
from typing import Dict, List
from dataclasses import dataclass


@dataclass
class IntegrityFactors:
    """Individual integrity factors"""
    drift_severity: float  # 0-1
    anomaly_count: int
    baseline_confidence: float  # 0-1
    correlation_stability: float  # 0-1
    variance_stability: float  # 0-1
    response_stability: float  # 0-1


class IntegrityScoreEngine:
    """
    Composite integrity scoring with smooth decay/recovery
    Score: 0-100 (100 = perfect integrity)
    
    Mathematical formula:
    Score = base_score * (1 - drift_penalty) * (1 - anomaly_penalty) * stability_factor
    
    Where:
    - drift_penalty = drift_severity * weight
    - anomaly_penalty = min(anomaly_count * weight, max_penalty)
    - stability_factor = weighted_average(correlation, variance, response)
    """
    
    def __init__(self):
        # Weights for composite scoring
        self.weights = {
            "drift": 0.4,          # Drift has highest impact
            "anomaly": 0.3,        # Sudden anomalies second
            "correlation": 0.15,   # Relationship stability
            "variance": 0.10,      # Distribution stability
            "response": 0.05       # System responsiveness
        }
        
        # Scoring parameters
        self.base_score = 100.0
        self.current_score = 100.0
        self.decay_rate = 2.0      # Points lost per severe drift event
        self.recovery_rate = 0.5   # Points recovered per stable interval
        self.min_score = 0.0
        self.max_score = 100.0
        
        # Smoothing
        self.score_history = []
        self.smooth_window = 5
        
    def calculate(self, factors: IntegrityFactors) -> Dict:
        """
        Calculate composite integrity score
        
        Returns:
            {
                "score": float (0-100),
                "change": float (delta from last),
                "factors": dict of individual factor contributions,
                "severity": str (STABLE/WARNING/CRITICAL)
            }
        """
        
        # Calculate individual penalties
        drift_penalty = factors.drift_severity * self.weights["drift"] * 100
        
        anomaly_penalty = min(
            factors.anomaly_count * 10 * self.weights["anomaly"],
            30  # Max 30 points from anomalies
        )
        
        # Calculate stability factor (0-1)
        stability_factor = (
            factors.correlation_stability * self.weights["correlation"] +
            factors.variance_stability * self.weights["variance"] +
            factors.response_stability * self.weights["response"]
        ) / (self.weights["correlation"] + self.weights["variance"] + self.weights["response"])
        
        stability_bonus = stability_factor * 10  # Up to 10 bonus points
        
        # Compute raw score
        raw_score = self.base_score - drift_penalty - anomaly_penalty + stability_bonus
        
        # Apply smooth decay/recovery
        if raw_score < self.current_score:
            # Decaying - use decay rate
            self.current_score = max(
                raw_score,
                self.current_score - self.decay_rate
            )
        else:
            # Recovering - use recovery rate
            self.current_score = min(
                raw_score,
                self.current_score + self.recovery_rate
            )
        
        # Clamp to bounds
        self.current_score = np.clip(self.current_score, self.min_score, self.max_score)
        
        # Smooth with moving average
        self.score_history.append(self.current_score)
        if len(self.score_history) > self.smooth_window:
            self.score_history.pop(0)
        
        smoothed_score = np.mean(self.score_history)
        
        # Calculate change
        change = 0.0
        if len(self.score_history) > 1:
            change = self.score_history[-1] - self.score_history[-2]
        
        # Determine severity
        severity = self._classify_severity(smoothed_score)
        
        # Factor breakdown
        factor_breakdown = {
            "drift_impact": -drift_penalty,
            "anomaly_impact": -anomaly_penalty,
            "stability_bonus": stability_bonus,
            "correlation_score": factors.correlation_stability * 100,
            "variance_score": factors.variance_stability * 100
        }
        
        return {
            "score": round(smoothed_score, 2),
            "change": round(change, 2),
            "factors": factor_breakdown,
            "severity": severity,
            "raw_score": round(raw_score, 2)
        }
    
    def _classify_severity(self, score: float) -> str:
        """Classify integrity state based on score"""
        if score >= 80:
            return "STABLE"
        elif score >= 50:
            return "WARNING"
        else:
            return "CRITICAL"
    
    def reset(self):
        """Reset to perfect integrity"""
        self.current_score = 100.0
        self.score_history = []
