from functools import lru_cache


def longest_common_subarray_dp(A, B):
    m, n = len(A), len(B)
    # Initialize a 2D table with zeros
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    max_len = 0

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if A[i - 1] == B[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                max_len = max(max_len, dp[i][j])
            else:
                # Contiguous requirement: reset to 0 if no match
                dp[i][j] = 0

    return max_len


def longest_common_subarray(A, B):

    @lru_cache(None)
    def solve(i, j):
        if i < 0 or j < 0:
            # (ending_here, overall_max)
            return 0, 0

        # Get results from sub-problems
        _, max_left = solve(i - 1, j)
        _, max_up = solve(i, j - 1)

        # Calculate match ending exactly here
        ending_here = (1 + solve(i - 1, j - 1)[0]) if A[i] == B[j] else 0

        # The new overall max is the best of all three directions
        overall_max = max(ending_here, max_left, max_up)

        return ending_here, overall_max

    return solve(len(A) - 1, len(B) - 1)[1]


class TrieNode:
    def __init__(self):
        self.children = {}
        self.word = None  # Stores the full word at the leaf node


def find_words(board, words):
    # 1. Build the Trie
    root = TrieNode()
    for w in words:
        node = root
        for char in w:
            node = node.children.setdefault(char, TrieNode())
        node.word = w

    rows, cols = len(board), len(board[0])
    results = []

    def backtrack(r, c, parent):
        char = board[r][c]
        curr_node = parent.children[char]

        # Found a word!
        if curr_node.word:
            results.append(curr_node.word)
            curr_node.word = None  # Avoid duplicates

        # Mark as visited
        board[r][c] = "#"

        # Explore neighbors
        for dr, dc in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
            nr, nc = r + dr, c + dc
            if (
                0 <= nr < rows
                and 0 <= nc < cols
                and board[nr][nc] in curr_node.children
            ):
                backtrack(nr, nc, curr_node)

        # Backtrack: Restore the board
        board[r][c] = char

        # Optimization: Prune leaf nodes to speed up the search
        if not curr_node.children:
            parent.children.pop(char)

    # 2. Start DFS from every cell that is a start of any word in Trie
    for r in range(rows):
        for c in range(cols):
            if board[r][c] in root.children:
                backtrack(r, c, root)

    return results


if __name__ == "__main__":
    lst1, lst2 = ["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e", "f"]
    print(longest_common_subarray(lst1, lst2))
    print(longest_common_subarray_dp(lst1, lst2))
