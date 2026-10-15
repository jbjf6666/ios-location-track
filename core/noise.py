"""真实感定位模型：多尺度 GPS 误差 + 发送节奏抖动 + 运动学速度。

为什么不用 random()：真实手机的 GPS 误差是**缓慢游走**的（受卫星几何、多径、电离层影响），
相邻两次定位通常只差几十厘米到一两米，而且有惯性。如果每个点独立随机跳，轨迹会变成毛刺，
速度和航向也会剧烈抖动——这类特征在验收测试里会掩盖真实问题（也有经验的风控一眼能看出）。

这里用**值噪声（value noise）+ 平滑插值 + 多尺度叠加**建模：
- **多尺度误差**：真实 GPS 误差是幂律分布（Allan 方差），不是单一尺度。
  这里用 3 个八度叠加：慢漂（相关长度 ~120 米，多径/电离层）+ 中尺度（~30 米，卫星几何）
  + 快抖（~6 米，接收机噪声）。单尺度噪声的统计特征一眼可辨。
- **发送节奏抖动**：真实 GPS 回调不是精确 1.000 秒，而是 0.85~1.15 秒抖动，
  偶尔丢一次采样。节奏完全均匀是最容易被识别的特征之一。
- **速度由位移/时间推算**：这与 iOS 从坐标序列推算 speed 的方式一致，
  因此"上报速度"与"实际位移"天然自洽，不会出现自相矛盾的数据。
- 纯函数式、无状态、可复现：同样的距离轴/时间轴永远得到同样的噪声值，便于回归测试。
"""
from __future__ import annotations

import math

# 相关长度：多少米走完一个完整的"漂移周期"
DEFAULT_CORRELATION_M = 30.0
# 默认漂移半径（米）：手机 GPS 在城市环境下的典型误差量级
DEFAULT_DRIFT_M = 4.0
DEFAULT_DRIFT_PERIOD_S = 18.0
# 默认速度浮动幅度：±8%
DEFAULT_SPEED_JITTER = 0.08

MAX_DRIFT_M = 50.0
MAX_SPEED_JITTER = 0.6

# 多尺度误差的三个八度：(相对权重, 相关长度倍数)
# 慢漂:中尺度:快抖 ≈ 0.5 : 0.33 : 0.17，接近 GPS 误差的幂律谱。
# 快抖的相关长度不能太短：取 0.5×30m = 15m。若取 6~10m，1Hz 下每秒误差变化可达
# 6~8 米，位移推算出的"瞬时速度"会明显超出步行/跑步范围（真实接收机内部对速度做过平滑）。
ERROR_OCTAVES = ((0.5, 4.0), (0.34, 1.0), (0.16, 0.5))
# 叠加后的幅值归一化系数：实测三个尺度加权叠加的 RMS 约为单尺度的 0.55 倍，
# 除以该系数即可让 drift_m 参数与"典型偏移半径"一致（数值由测试标定）。
OCTAVE_NORMALIZER = 1.0 / 0.55
# 单个采样的偏移上限（相对 radius 的倍数）：真实 GPS 误差有长尾，但不会无限大
MAX_OFFSET_FACTOR = 1.8

# 发送间隔抖动：真实 1Hz GPS 的实际间隔分布
DEFAULT_TIMING_JITTER = 0.15      # ±15%
DROPOUT_PROBABILITY = 0.02        # 约 2% 的采样会丢一次（变成 2 秒间隔）
MIN_INTERVAL_FACTOR = 0.3         # 抖动下限保护（避免退化成高频）


def _hash01(seed: int, index: int) -> float:
    """把 (seed, index) 映射成 [0,1) 的确定性伪随机数（无需 numpy）。"""
    x = (index * 374761393 + seed * 668265263) & 0xFFFFFFFF
    x = (x ^ (x >> 13)) * 1274126177 & 0xFFFFFFFF
    x = x ^ (x >> 16)
    return (x & 0xFFFFFF) / float(0x1000000)


def _smooth(t: float) -> float:
    """smoothstep：让分段插值在节点处一阶连续，避免折角。"""
    return t * t * (3.0 - 2.0 * t)


def value_noise(seed: int, x: float) -> float:
    """一维值噪声，返回 [-1, 1]，在 x 的整数节点间平滑插值。"""
    i0 = math.floor(x)
    frac = x - i0
    a = _hash01(seed, i0) * 2.0 - 1.0
    b = _hash01(seed, i0 + 1) * 2.0 - 1.0
    return a + (b - a) * _smooth(frac)


def fbm(seed: int, x: float, octaves: int = 2) -> float:
    """两个八度的分形噪声，归一化到 [-1, 1]。"""
    total = 0.0
    amplitude = 1.0
    frequency = 1.0
    norm = 0.0
    for octave in range(max(1, octaves)):
        total += value_noise(seed + octave * 7919, x * frequency) * amplitude
        norm += amplitude
        amplitude *= 0.5
        frequency *= 2.3
    return total / norm if norm else 0.0


class DriftGenerator:
    """按"沿轨迹行进的距离"生成平滑漂移偏移量与速度浮动系数。

    - 位置漂移：沿行进方向与垂直方向各一份噪声，合成横向摆动（GPS 误差的常见形态）；
    - 速度浮动：乘性系数，围绕 1.0 平滑波动，让瞬时速度在设定值上下浮动。
    """

    def __init__(self, seed: int = 20261014) -> None:
        self.seed = int(seed)

    # ---------------- 位置漂移 ----------------
    def offset_m(self, distance_m: float, drift_m: float,
                 correlation_m: float = DEFAULT_CORRELATION_M,
                 octaves: bool = True) -> tuple[float, float]:
        """返回 (沿行进方向偏移, 垂直方向偏移)，单位米。

        octaves=True 时叠加 3 个尺度的误差（慢漂 + 中尺度 + 快抖），
        更接近真实 GPS 误差的幂律谱；False 时退化为单一尺度（旧行为）。
        """
        if drift_m <= 0:
            return 0.0, 0.0
        scale = max(5.0, correlation_m)
        if not octaves:
            x = distance_m / scale
            along = fbm(self.seed, x) * drift_m
            cross = fbm(self.seed + 104729, x + 0.5) * drift_m
            return along, cross
        along = 0.0
        cross = 0.0
        for index, (weight, factor) in enumerate(ERROR_OCTAVES):
            x = distance_m / (scale * factor)
            # 每个尺度用不同种子与相位偏移，避免各尺度同相叠加成规律波形
            along += value_noise(self.seed + index * 1013, x) * weight
            cross += value_noise(self.seed + 104729 + index * 7919, x + 0.37 * (index + 1)) * weight
        # 多尺度叠加后幅值会明显小于单尺度（各尺度并不总是同相），
        # 这里归一化，保证 drift_m 参数就是"典型偏移半径"，与用户设定一致。
        along *= OCTAVE_NORMALIZER
        cross *= OCTAVE_NORMALIZER
        # 合成偏移做上限裁剪：真实 GPS 误差有长尾，但不会无限大，
        # 否则偶发的大偏移会被下游 App 当成"跳点"整段丢弃。
        magnitude = math.hypot(along, cross)
        if magnitude > MAX_OFFSET_FACTOR:
            shrink = MAX_OFFSET_FACTOR / magnitude
            along *= shrink
            cross *= shrink
        return along * drift_m, cross * drift_m

    def offset_latlng(self, lat: float, lng: float, distance_m: float,
                      heading_deg: float, drift_m: float,
                      correlation_m: float = DEFAULT_CORRELATION_M) -> tuple[float, float]:
        """把米制偏移换算成经纬度偏移（按当前纬度修正经度比例）。"""
        along, cross = self.offset_m(distance_m, drift_m, correlation_m)
        if along == 0.0 and cross == 0.0:
            return 0.0, 0.0
        rad = math.radians(heading_deg)
        # 行进方向单位向量（x=东, y=北）
        fx, fy = math.sin(rad), math.cos(rad)
        # 垂直方向（右手侧）
        px, py = fy, -fx
        east_m = along * fx + cross * px
        north_m = along * fy + cross * py
        dlat = north_m / 111320.0
        cos_lat = math.cos(math.radians(max(-85.0, min(85.0, lat))))
        dlng = east_m / (111320.0 * max(0.05, cos_lat))
        return dlat, dlng

    # ---------------- 速度浮动 ----------------
    def speed_factor(self, elapsed_s: float, jitter: float,
                     period_s: float = DEFAULT_DRIFT_PERIOD_S) -> float:
        """返回速度乘性系数（1.0 表示不浮动）。

        对应真实跑步中步频/步幅的自然波动与路况影响：
        用 2 个八度的低频噪声（不是正弦，因此没有可识别的周期）。
        """
        if jitter <= 0:
            return 1.0
        x = elapsed_s / max(2.0, period_s)
        value = fbm(self.seed + 15485863, x + 3.25)
        factor = 1.0 + value * jitter
        # 防止极端情况下出现 0 或负速度
        return max(0.2, min(2.0, factor))

    # ---------------- 发送节奏（真实 GPS 不是精确等间隔） ----------------
    def timing_factor(self, sample_index: int, jitter: float = DEFAULT_TIMING_JITTER,
                      dropouts: bool = True) -> float:
        """返回本次发送间隔的乘性系数。

        真实 1Hz GPS 的实际间隔在 0.85~1.15 秒之间抖动，偶发丢采样时变成约 2 秒。
        间隔完全均匀（精确 1.000s）在数据上非常"人工"，部分验收用例甚至会据此告警。
        """
        if jitter <= 0:
            return 1.0
        # 用噪声而不是 random：连续几次采样的间隔会小幅相关，像真实接收机
        value = value_noise(self.seed + 777771, sample_index * 0.35)
        factor = 1.0 + value * jitter
        # 丢采样是独立随机事件（不能用平滑噪声表达），用哈希伪随机
        if dropouts and _hash01(self.seed + 31337, sample_index) < DROPOUT_PROBABILITY:
            factor += 1.0  # 丢一次采样 → 间隔约翻倍（真实 GPS 偶发情况）
        return max(MIN_INTERVAL_FACTOR, min(2.5, factor))

    # ---------------- 粗差（真实 GPS 偶发的坏点） ----------------
    def gross_error_m(self, sample_index: int, probability: float = 0.0,
                      magnitude_m: float = 30.0) -> tuple[float, float]:
        """按概率生成一次"粗差"偏移（多径/城市峡谷导致的坏点）。

        默认关闭（probability=0）。开启后可用来测你自己 App 的
        `horizontalAccuracy` 过滤与跳点剔除逻辑是否生效。
        """
        if probability <= 0:
            return 0.0, 0.0
        roll = _hash01(self.seed + 5150, sample_index)
        if roll >= probability:
            return 0.0, 0.0
        # 坏点方向随机、幅度在 0.5~1.5 倍之间
        angle = _hash01(self.seed + 6161, sample_index) * 2.0 * math.pi
        scale = magnitude_m * (0.5 + _hash01(self.seed + 7171, sample_index))
        return math.cos(angle) * scale, math.sin(angle) * scale


_generator = DriftGenerator()


def default_generator() -> DriftGenerator:
    return _generator
