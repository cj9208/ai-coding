class HitCounter:
    def __init__(self):
        # 300 buckets for 300 seconds
        self.capacity = 300
        self.hits = [0] * self.capacity
        self.times = [0] * self.capacity

    def hit(self, timestamp: int) -> None:
        idx = timestamp % self.capacity
        # If this is a new "cycle" for this bucket, reset it
        if self.times[idx] != timestamp:
            self.times[idx] = timestamp
            self.hits[idx] = 1
        else:
            self.hits[idx] += 1

    def getHits(self, timestamp: int) -> int:
        total_hits = 0
        for i in range(self.capacity):
            # Only count buckets that are within the 5-minute window
            if timestamp - self.times[i] < 300:
                total_hits += self.hits[i]
        return total_hits
