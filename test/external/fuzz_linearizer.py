import random
import numpy as np
from collections import Counter, defaultdict
from extra.optimization.helpers import load_worlds, ast_str_to_lin
from tinygrad.codegen.linearizer import Linearizer
from tinygrad.features.search import get_linearizer_actions
from tinygrad.graph import print_tree
from tinygrad.helpers import prod
from tinygrad.ops import Device, Compiled

random.seed(42)
np.random.seed(42)
device = Device[Device.DEFAULT]

class LB:
  # placeholder LazyBuffer
  def __init__(self, rawbuf, dtype):
    self.realized = rawbuf
    self.output_buffer = rawbuf
    self.dtype = dtype


def fuzz_linearizer(lin: Linearizer):
  print_tree(lin.ast)
  print(lin.colored_shape())

  rawbufs = [None]  # first one is output
  rawbuf_size = defaultdict(int)
  for buf in lin.membufs[1:]:
    idx, valid = buf.st.expr_idxs()
    # TODO: image type and variable shape
    size = idx.max+1
    rawbuf_size[buf.idx] = max(rawbuf_size[buf.idx], size)

  for i, size in sorted(rawbuf_size.items()):
    assert len(rawbufs) == i
    # TODO: different range for int type v.s. float type
    rawbuf = device.buffer.fromCPU(np.random.uniform(low=-5.0, high=5.0, size=size).astype(buf.dtype.np))
    rawbufs.append(rawbuf)

  # NOTE: copied from beam_search
  def tuplize_uops(uops): return tuple([(x.uop, x.dtype, tuple(x.num for x in x.vin), x.arg) for x in uops])
  seen_uops = {}

  ground_truth = None
  while 1:
    if len(seen_uops) >= 20: break  # enough for this kernel
    # TODO: if this is too slow, we can reject sample until first valid action, instead of getting all actions first
    actions = get_linearizer_actions(lin.copy(), include_0=False)
    if not actions: break
    lin = random.choice(list(actions.values()))
    if lin.applied_opts: print(f"last action: {lin.applied_opts[-1]}")

    # stop if kernel uops repeat
    tuops = tuplize_uops(lin.copy().linearize().uops)
    if tuops in seen_uops: break
    seen_uops[tuops] = tuple(lin.applied_opts)

    print(lin.colored_shape())
    # get a new output buffer
    rawbufs[0] = device.buffer(size=prod(lin.membufs[0].st.shape), dtype=lin.membufs[0].dtype)

    if isinstance(device, Compiled):
      try:
        prg = device.to_program(lin.copy())
      except:
        import traceback
        traceback.print_exc()
        print("COMPILE FAILED!!")
        return "COMPILE_ERROR"
      try:
        prg.exec(rawbufs, force_wait=True)
      except:
        print("EXEC FAILED!!")
        return "EXEC_ERROR"
    else:
      # TODO: Interpreted does not work with symbolic shape
      try:
        device.exec_ast(lin.ast, output=LB(rawbufs[0], rawbufs[0].dtype), inputs=[LB(buf, buf.dtype) for buf in rawbufs[1:]])
      except Exception as e:
        import traceback
        traceback.print_exc()
        return str(type(e))

    result = rawbufs[0].toCPU()

    if ground_truth is None:
      ground_truth = result
    else:
      try:
        # TODO: assert based on L2 distance not elementwise
        np.testing.assert_allclose(result, ground_truth, rtol=1e-4, atol=1e-4)
      except AssertionError:
        return "NOT_ALLCLOSE"
      except Exception as e:
        import traceback
        traceback.print_exc()
        return str(type(e))
  return "PASS"

CPU_FAILED = [19, 27, 52, 54, 66, 79, 91, 116, 120, 127, 149, 166, 208, 221, 243, 260, 272, 282, 317, 329, 335, 340, 380, 381, 403, 411, 412, 416, 417, 447, 462, 472, 495, 504, 524, 548, 552, 567, 576, 583, 584, 590, 591, 612, 625, 631, 685, 686, 687, 695, 705, 706, 722, 760, 762, 774, 781, 801, 822, 844, 847, 881, 908, 910, 960, 971, 975, 982, 1007, 1023, 1039, 1045, 1073, 1083, 1095, 1105, 1119, 1136, 1146, 1167, 1199, 1236, 1245, 1252, 1254, 1281, 1321, 1327, 1336, 1346, 1403, 1424, 1438, 1455, 1476, 1500, 1509, 1588, 1594, 1600, 1601, 1606, 1626, 1636, 1637, 1643, 1657, 1669, 1678, 1688, 1723, 1727, 1740, 1748, 1782, 1798, 1826, 1836, 1851, 1860, 1873, 1896, 1906, 1920, 1957, 1963, 1964, 2013, 2026, 2084, 2101, 2108, 2114, 2119, 2121, 2144, 2151, 2152, 2242, 2251, 2254, 2259, 2266, 2286, 2290, 2298, 2343, 2355, 2365, 2378, 2391, 2412, 2418, 2445, 2450, 2458, 2469, 2477, 2504, 2531, 2546, 2552, 2560, 2579, 2588, 2595, 2668, 2681, 2705, 2727, 2737, 2742, 2761, 2778, 2799, 2811, 2826, 2853, 2861, 2867, 2872, 2882, 2883, 2891, 2903, 2917, 2923, 2935, 2941, 2950, 2956, 2965, 2969, 2997, 2998, 3000, 3001, 3009, 3015, 3017, 3037, 3046, 3068, 3104, 3109, 3112, 3117, 3126, 3127, 3131, 3132, 3156, 3160, 3184, 3242, 3247, 3254, 3258, 3259, 3325, 3328, 3359, 3364, 3372, 3390, 3402, 3415, 3420, 3429, 3441, 3476, 3501, 3506, 3511, 3523, 3535, 3550]
TORCH_FAILED = CPU_FAILED
CLANG_FAILED = [0, 2, 3, 4, 6, 7, 11, 16, 19, 23, 26, 27, 28, 29, 30, 34, 35, 37, 39, 42, 43, 45, 46, 48, 50, 52, 55, 58, 60, 64, 66, 67, 69, 72, 74, 78, 84, 86, 87, 89, 99, 103, 104, 105, 106, 108, 109, 110, 111, 112, 115, 117, 118, 119, 120, 126, 127, 129, 131, 132, 138, 142, 143, 147, 148, 151, 158, 160, 165, 167, 170, 175, 176, 179, 180, 183, 188, 201, 202, 207, 208, 210, 219, 220, 222, 223, 228, 229, 231, 233, 234, 235, 236, 237, 239, 240, 241, 246, 247, 250, 251, 253, 255, 257, 258, 259, 260, 261, 264, 265, 266, 267, 268, 269, 273, 274, 278, 282, 283, 290, 291, 295, 298, 300, 301, 302, 303, 309, 311, 314, 315, 319, 320, 323, 325, 331, 332, 333, 334, 338, 343, 346, 348, 352, 353, 356, 368, 370, 371, 374, 376, 377, 382, 386, 387, 388, 393, 396, 400, 403, 404, 407, 408, 409, 410, 412, 415, 417, 418, 419, 421, 422, 423, 425, 426, 438, 439, 444, 445, 446, 451, 455, 460, 464, 465, 469, 472, 477, 480, 481, 483, 488, 489, 490, 497, 498, 501, 502, 506, 507, 508, 510, 515, 519, 520, 523, 524, 525, 530, 535, 536, 537, 538, 540, 543, 544, 545, 546, 552, 553, 557, 561, 566, 569, 570, 571, 575, 578, 582, 585, 586, 587, 589, 592, 594, 597, 599, 600, 602, 604, 606, 608, 611, 613, 619, 620, 622, 624, 626, 627, 632, 634, 635, 638, 639, 641, 642, 646, 648, 650, 656, 657, 659, 660, 661, 665, 666, 667, 669, 673, 674, 681, 686, 689, 692, 693, 696, 703, 708, 709, 711, 712, 714, 715, 716, 719, 720, 725, 726, 728, 730, 733, 734, 735, 736, 737, 739, 742, 746, 750, 754, 759, 761, 763, 766, 767, 768, 770, 772, 776, 777, 779, 783, 784, 786, 790, 792, 793, 794, 796, 801, 803, 804, 807, 810, 815, 818, 819, 820, 822, 829, 830, 833, 834, 835, 837, 838, 841, 846, 848, 849, 851, 852, 854, 859, 862, 865, 869, 870, 871, 878, 881, 883, 885, 887, 889, 892, 893, 895, 900, 901, 905, 907, 909, 912, 913, 918, 921, 922, 923, 925, 928, 930, 932, 933, 936, 937, 939, 940, 942, 943, 945, 948, 952, 955, 957, 959, 962, 963, 972, 976, 977, 978, 979, 983, 984, 992, 993, 1004, 1006, 1009, 1012, 1014, 1017, 1019, 1020, 1021, 1024, 1025, 1026, 1028, 1033, 1035, 1039, 1042, 1047, 1049, 1050, 1051, 1052, 1053, 1060, 1061, 1063, 1067, 1070, 1072, 1074, 1075, 1077, 1080, 1082, 1085, 1087, 1090, 1091, 1094, 1095, 1096, 1099, 1100, 1101, 1102, 1103, 1104, 1105, 1110, 1113, 1119, 1120, 1121, 1126, 1128, 1130, 1131, 1133, 1134, 1137, 1144, 1147, 1148, 1151, 1152, 1155, 1156, 1158, 1159, 1162, 1165, 1167, 1169, 1174, 1181, 1189, 1190, 1194, 1197, 1206, 1207, 1212, 1216, 1218, 1219, 1220, 1221, 1223, 1224, 1226, 1228, 1230, 1231, 1233, 1234, 1238, 1245, 1249, 1250, 1254, 1257, 1262, 1263, 1265, 1267, 1270, 1271, 1276, 1278, 1279, 1288, 1292, 1293, 1294, 1303, 1312, 1322, 1323, 1328, 1329, 1330, 1331, 1332, 1335, 1338, 1339, 1342, 1347, 1351, 1356, 1362, 1371, 1375, 1378, 1380, 1382, 1387, 1388, 1390, 1392, 1396, 1402, 1404, 1405, 1406, 1409, 1411, 1413, 1414, 1415, 1416, 1417, 1418, 1419, 1421, 1422, 1423, 1424, 1427, 1436, 1437, 1439, 1440, 1441, 1442, 1443, 1448, 1450, 1451, 1452, 1459, 1462, 1465, 1472, 1474, 1478, 1479, 1482, 1483, 1484, 1486, 1487, 1488, 1492, 1494, 1495, 1496, 1497, 1499, 1504, 1508, 1511, 1512, 1513, 1514, 1519, 1524, 1526, 1528, 1532, 1533, 1535, 1537, 1539, 1544, 1545, 1550, 1552, 1553, 1554, 1555, 1557, 1561, 1565, 1567, 1568, 1569, 1573, 1574, 1576, 1578, 1580, 1582, 1583, 1584, 1585, 1597, 1598, 1602, 1608, 1613, 1614, 1615, 1618, 1621, 1624, 1625, 1629, 1631, 1637, 1638, 1642, 1643, 1645, 1649, 1655, 1656, 1659, 1661, 1663, 1664, 1665, 1669, 1670, 1673, 1677, 1678, 1682, 1683, 1696, 1698, 1702, 1703, 1706, 1707, 1708, 1711, 1712, 1713, 1716, 1717, 1719, 1720, 1722, 1727, 1731, 1735, 1737, 1738, 1739, 1741, 1742, 1743, 1745, 1746, 1748, 1755, 1763, 1765, 1766, 1770, 1775, 1776, 1777, 1778, 1779, 1780, 1787, 1788, 1793, 1795, 1798, 1805, 1806, 1810, 1811, 1814, 1817, 1818, 1820, 1825, 1826, 1827, 1831, 1832, 1837, 1841, 1844, 1846, 1848, 1849, 1851, 1852, 1854, 1861, 1862, 1865, 1867, 1869, 1870, 1871, 1872, 1875, 1877, 1882, 1885, 1889, 1890, 1891, 1892, 1894, 1896, 1897, 1898, 1900, 1901, 1904, 1909, 1910, 1913, 1914, 1919, 1922, 1923, 1924, 1925, 1926, 1928, 1930, 1934, 1939, 1941, 1942, 1943, 1944, 1945, 1947, 1950, 1951, 1956, 1958, 1963, 1967, 1969, 1970, 1971, 1972, 1973, 1974, 1977, 1979, 1983, 1984, 1985, 1992, 1994, 1995, 1996, 1999, 2000, 2001, 2002, 2005, 2007, 2011, 2013, 2016, 2041, 2042, 2043, 2044, 2046, 2051, 2056, 2059, 2063, 2064, 2066, 2070, 2074, 2075, 2077, 2079, 2083, 2085, 2087, 2088, 2091, 2097, 2100, 2104, 2107, 2109, 2111, 2112, 2115, 2117, 2119, 2124, 2125, 2126, 2127, 2130, 2131, 2136, 2146, 2147, 2148, 2151, 2152, 2155, 2160, 2162, 2163, 2167, 2171, 2174, 2178, 2179, 2183, 2185, 2187, 2188, 2190, 2191, 2193, 2199, 2202, 2210, 2211, 2212, 2213, 2215, 2216, 2217, 2221, 2226, 2234, 2239, 2242, 2244, 2245, 2247, 2248, 2249, 2250, 2253, 2258, 2261, 2264, 2268, 2271, 2274, 2279, 2281, 2283, 2285, 2289, 2290, 2291, 2295, 2299, 2301, 2307, 2311, 2312, 2314, 2318, 2321, 2324, 2325, 2327, 2329, 2331, 2333, 2336, 2343, 2347, 2348, 2349, 2360, 2376, 2380, 2382, 2392, 2394, 2396, 2400, 2403, 2408, 2410, 2412, 2414, 2416, 2421, 2423, 2424, 2425, 2430, 2431, 2432, 2433, 2440, 2441, 2443, 2446, 2447, 2448, 2450, 2455, 2457, 2467, 2472, 2474, 2476, 2477, 2478, 2479, 2481, 2485, 2489, 2493, 2495, 2496, 2497, 2500, 2502, 2503, 2505, 2506, 2508, 2509, 2514, 2515, 2517, 2519, 2521, 2523, 2528, 2529, 2532, 2533, 2537, 2538, 2541, 2543, 2546, 2547, 2550, 2552, 2553, 2554, 2555, 2556, 2557, 2558, 2559, 2567, 2569, 2572, 2581, 2582, 2584, 2586, 2587, 2590, 2591, 2593, 2594, 2598, 2600, 2601, 2604, 2605, 2614, 2615, 2619, 2624, 2625, 2626, 2627, 2632, 2633, 2634, 2637, 2643, 2647, 2651, 2652, 2655, 2657, 2659, 2663, 2664, 2667, 2670, 2671, 2673, 2674, 2675, 2678, 2682, 2683, 2686, 2687, 2689, 2691, 2693, 2695, 2700, 2701, 2702, 2704, 2710, 2711, 2719, 2724, 2729, 2732, 2734, 2736, 2739, 2742, 2747, 2748, 2754, 2760, 2764, 2766, 2767, 2768, 2769, 2782, 2787, 2790, 2795, 2800, 2807, 2811, 2812, 2817, 2821, 2822, 2824, 2830, 2831, 2832, 2833, 2835, 2836, 2838, 2844, 2846, 2848, 2849, 2850, 2851, 2852, 2853, 2854, 2855, 2856, 2857, 2858, 2860, 2861, 2863, 2867, 2868, 2871, 2873, 2875, 2878, 2879, 2885, 2887, 2893, 2896, 2897, 2902, 2904, 2908, 2909, 2912, 2914, 2918, 2922, 2930, 2931, 2934, 2935, 2938, 2943, 2944, 2945, 2946, 2949, 2950, 2951, 2953, 2954, 2955, 2957, 2963, 2965, 2972, 2975, 2980, 2984, 2985, 2987, 2991, 2992, 2993, 2994, 2998, 3006, 3012, 3016, 3020, 3022, 3023, 3024, 3027, 3029, 3030, 3039, 3040, 3041, 3045, 3049, 3051, 3057, 3059, 3064, 3067, 3068, 3069, 3074, 3080, 3085, 3088, 3089, 3095, 3098, 3100, 3101, 3102, 3106, 3111, 3114, 3116, 3118, 3121, 3122, 3125, 3127, 3130, 3138, 3139, 3143, 3145, 3146, 3152, 3155, 3156, 3159, 3160, 3165, 3166, 3167, 3168, 3169, 3170, 3171, 3172, 3174, 3177, 3180, 3183, 3192, 3196, 3197, 3198, 3199, 3200, 3202, 3203, 3210, 3215, 3217, 3218, 3220, 3225, 3226, 3228, 3229, 3230, 3231, 3232, 3236, 3237, 3239, 3243, 3248, 3250, 3256, 3259, 3263, 3268, 3273, 3274, 3276, 3280, 3284, 3285, 3286, 3288, 3293, 3296, 3299, 3300, 3301, 3306, 3308, 3310, 3312, 3315, 3317, 3318, 3319, 3323, 3324, 3326, 3329, 3340, 3343, 3344, 3350, 3351, 3360, 3364, 3365, 3367, 3370, 3374, 3385, 3388, 3391, 3394, 3395, 3396, 3402, 3405, 3406, 3408, 3409, 3410, 3412, 3413, 3416, 3419, 3422, 3423, 3425, 3431, 3435, 3437, 3438, 3443, 3444, 3448, 3449, 3450, 3452, 3453, 3458, 3459, 3468, 3471, 3473, 3481, 3482, 3484, 3486, 3487, 3489, 3491, 3492, 3497, 3502, 3504, 3506, 3509, 3515, 3521, 3530, 3535, 3540, 3546, 3549, 3551, 3552]
# only tested 500
METAL_FAILED = [0, 6, 19, 27, 35, 39, 50, 52, 60, 66, 80, 86, 89, 94, 101, 105, 106, 112, 115, 118, 119, 120, 147, 162, 164, 165, 170, 176, 186, 202, 208, 210, 223, 228, 236, 241, 250, 251, 253, 258, 260, 261, 263, 266, 268, 278, 282, 291, 293, 295, 298, 307, 311, 314, 323, 325, 327, 332, 338, 343, 345, 349, 353, 371, 374, 376, 385, 387, 392, 397, 404, 408, 409, 415, 421, 433, 460, 463, 472, 480, 481, 482, 483, 488, 497]
GPU_FAILED = [0, 6, 19, 27, 35, 39, 50, 52, 60, 80, 84, 105, 106, 108, 115, 118, 119, 120, 126, 129, 165, 167, 170, 178, 186, 199, 201, 208, 210, 220, 223, 227, 236, 237, 240, 241, 250, 251, 258, 260, 261, 266, 268, 278, 282, 293, 294, 295, 298, 302, 311, 323, 327, 332, 338, 343, 345, 359, 371, 374, 377, 380, 382, 393, 396, 400, 403, 412, 415, 444, 460, 463, 465, 471, 472, 477, 480, 482, 487, 488]

if __name__ == "__main__":
  ast_strs = load_worlds()
  print(f"{len(ast_strs)=}")
  tested = 0
  c = Counter()
  failed = []
  # TODO: ast_strs[0] output contains nan?
  for i, ast in enumerate(ast_strs):
    print(f"testing ast {i}")
    tested += 1
    lin = ast_str_to_lin(ast)
    fuzz = fuzz_linearizer(lin)
    c[fuzz] += 1
    if fuzz != "PASS":
      failed.append(i)
  print(f"{tested=}")
  print(c.most_common())
  print(f"{failed=}")