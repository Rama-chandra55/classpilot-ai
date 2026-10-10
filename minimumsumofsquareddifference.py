class Solution:
    def minSumSquareDiff(self, n1: list[int], n2: list[int], k1: int, k2: int) -> int:
        k = k1 + k2
        f = [0] * 100001
        
        for a, b in zip(n1, n2):
            f[abs(a - b)] += 1
            
        for d in range(100000, 0, -1):
            if not f[d]: continue
            take = min(k, f[d])
            f[d] -= take
            f[d - 1] += take
            k -= take
            if not k: break
            
        return sum(d * d * c for d, c in enumerate(f))
