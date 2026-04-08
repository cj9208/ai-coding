# To use bucket sort, we actually assume the size of list is not large
# or there are only a few distinct element.
# E.g. if all numbers are distinct, the extra memory is large,
# e.g. stream data or doesn't fit into memory

import heapq


def topkfrequent(nums, k):
    # 1. Count frequencies
    count = {}
    for n in nums:
        count[n] = 1 + count.get(n, 0)

    # 2. Bucket Sort: Index = Frequency, Value = List of numbers
    # Max possible frequency is the length of the array
    buckets = [[] for _ in range(len(nums) + 1)]
    for n, freq in count.items():
        buckets[freq].append(n)

    # 3. Pull from the end of buckets until we have K elements
    res = []
    for i in range(len(buckets) - 1, 0, -1):
        for n in buckets[i]:
            res.append(n)
            if len(res) == k:
                return res


def topkmin(nums, k):
    heap = []
    for n in nums:
        heapq.heappush(heap, n)

    for _ in range(k):
        v = heapq.heappop(heap)
    return v


def topkmax(nums, k):
    heap = []
    for n in nums:
        heapq.heappush(heap, -n)

    for _ in range(k):
        v = heapq.heappop(heap)
    return -v


def topkfrequent_heap(nums, k):
    counts = {}
    for n in nums:
        counts[n] = counts.get(n, 0) + 1
    print(counts)

    heap = []
    # min heap of size k, (freq, num)
    # we want top k frequent, so we pop the least frequent when size exceeds k
    for n, c in counts.items():
        heapq.heappush(heap, (c, n))
        if len(heap) > k:
            # print(heap)
            heapq.heappop(heap)

    return [val for freq, val in heap]


if __name__ == "__main__":
    print(topkmin([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5))
    # test with a not sorted list
    print(topkmin([10, 9, 8, 1, 2, 3, -1, 0, 4, 5, 6, 7], 5))

    lst = [
        1,
        2,
        2,
        3,
        3,
        3,
        4,
        4,
        4,
        4,
        6,
        6,
        6,
        6,
        6,
        7,
        7,
        7,
        7,
        7,
        7,
        7,
        7,
        8,
        8,
        8,
        8,
        8,
        8,
        8,
        8,
        8,
        8,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
        9,
    ]
    print(topkfrequent_heap(lst, 5))
    print(topkfrequent(lst, 5))
