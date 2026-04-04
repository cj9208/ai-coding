from collections import defaultdict, deque


def alienOrder(words):
    # 1. Initialize graph and indegree
    adj = defaultdict(set)
    indegree = {char: 0 for word in words for char in word}

    # 2. Build the graph
    for i in range(len(words) - 1):
        w1, w2 = words[i], words[i + 1]
        min_len = min(len(w1), len(w2))

        # Check for prefix edge case: ["abc", "ab"] is invalid
        if len(w1) > len(w2) and w1[:min_len] == w2[:min_len]:
            return ""

        for j in range(min_len):
            if w1[j] != w2[j]:
                if w2[j] not in adj[w1[j]]:
                    adj[w1[j]].add(w2[j])
                    indegree[w2[j]] += 1
                break  # Only the first difference matters

    # 3. BFS (Kahn's Algorithm)
    queue = deque([char for char in indegree if indegree[char] == 0])
    result = []

    while queue:
        char = queue.popleft()
        result.append(char)
        for neighbor in adj[char]:
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)

    # 4. If result length matches unique chars, return string; else cycle detected
    if len(result) < len(indegree):
        return ""
    return "".join(result)
