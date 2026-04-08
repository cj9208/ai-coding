"""
word search
1. basic version, search a word in a board, return true if found, false otherwise
2. find all words in a board, given a list of words, return the list of found words
3. the size of the board, and the length of the words can be large,
    so we need to optimize the search
3.1 DAWG (Directed Acyclic Word Graph) is a compressed version of Trie,
    it can save space and speed up the search
3.2 shard the board into smaller sub-boards, and search each sub-board with a Trie.
    The sub-board should overlap to avoid missing words that span across the boundary.
3.3 shard the word list into smaller sub-lists, and build a Trie for each sub-list.
"""


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
