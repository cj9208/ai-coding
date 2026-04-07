"""
1. we use a doubly linked list to store the nodes with the same frequency.
2. the head and tail of the list are dummy nodes for easy insertion and deletion.
3. we use a dictionary to store the nodes with the same key for quick lookup.
4. when we get a key, we move the node to the head of the list with the same
   frequency. this way, we can always find the most recently used node with
   the same frequency.
"""

from typing import Optional


class Node:
    def __init__(self, key: int, val: int):
        self.key = key
        self.val = val
        self.freq = 1
        self.prev: Optional[Node] = None
        self.next: Optional[Node] = None


class DoublyLinkedList:
    def __init__(self):
        self.head = Node(0, 0)
        self.tail = Node(0, 0)
        self.head.next = self.tail
        self.tail.prev = self.head
        self.size = 0

    def add_to_head(self, node: Node) -> None:
        """Add node right after the head."""
        next_node = self.head.next
        self.head.next = node
        node.prev = self.head
        node.next = next_node
        if next_node:
            next_node.prev = node
        self.size += 1

    def remove_node(self, node: Node) -> None:
        """Remove a specific node."""
        prev_node = node.prev
        next_node = node.next
        if prev_node:
            prev_node.next = next_node
        if next_node:
            next_node.prev = prev_node
        self.size -= 1

    def remove_tail(self) -> Optional[Node]:
        """Remove the node before the tail (least recently used in this frequency list)."""
        if self.size == 0:
            return None
        node = self.tail.prev
        if node:
            self.remove_node(node)
        return node

    def is_empty(self) -> bool:
        return self.size == 0


class LFUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.min_freq = 0
        self.key_to_node: dict[int, Node] = {}
        self.freq_to_list: dict[int, DoublyLinkedList] = {}

    def _update_freq(self, node: Node) -> None:
        """Update the frequency of a node and move it to the corresponding frequency list."""
        freq = node.freq
        if freq in self.freq_to_list:
            self.freq_to_list[freq].remove_node(node)

            if self.freq_to_list[freq].is_empty():
                del self.freq_to_list[freq]
                if self.min_freq == freq:
                    self.min_freq = freq + 1

        node.freq += 1
        if node.freq not in self.freq_to_list:
            self.freq_to_list[node.freq] = DoublyLinkedList()
        self.freq_to_list[node.freq].add_to_head(node)

    def get(self, key: int) -> int:
        if key not in self.key_to_node:
            return -1

        node = self.key_to_node[key]
        self._update_freq(node)
        return node.val

    def put(self, key: int, value: int) -> None:
        if self.capacity <= 0:
            return

        if key in self.key_to_node:
            node = self.key_to_node[key]
            node.val = value
            self._update_freq(node)
        else:
            if len(self.key_to_node) >= self.capacity:
                # Evict the least frequently used item (if tie, least recently used)
                if self.min_freq in self.freq_to_list:
                    min_freq_list = self.freq_to_list[self.min_freq]
                    evicted_node = min_freq_list.remove_tail()
                    if evicted_node:
                        del self.key_to_node[evicted_node.key]

            new_node = Node(key, value)
            self.key_to_node[key] = new_node
            self.min_freq = 1  # New node always has frequency 1
            if 1 not in self.freq_to_list:
                self.freq_to_list[1] = DoublyLinkedList()
            self.freq_to_list[1].add_to_head(new_node)
