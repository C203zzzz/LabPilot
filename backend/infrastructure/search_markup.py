"""Small, dependency-free tree for bounded search result markup."""
from html.parser import HTMLParser


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    @property
    def classes(self):
        return set((self.attrs.get('class') or '').split())

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        if self.tag in {'script', 'style'}:
            return ''
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)


class ResultTree(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def result_rows(markup, engine):
    tree = ResultTree(markup)
    for block in tree.root.walk():
        if engine != 'bing':
            continue
        if block.tag != 'li' or 'b_algo' not in block.classes:
            continue
        heading = next((n for n in block.walk() if n.tag == 'h2'), None)
        snippets = [n.text() for n in block.walk() if n.tag == 'p']
        if heading is None:
            continue
        anchor = next((n for n in heading.walk() if n.tag == 'a' and n.attrs.get('href')), None)
        if anchor is None:
            continue
        href = anchor.attrs['href']
        yield {'url': href, 'title': ' '.join(anchor.text().split()), 'snippet': ' '.join(' '.join(snippets).split())}
