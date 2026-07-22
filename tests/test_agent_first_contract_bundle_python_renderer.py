from __future__ import annotations

import unittest

from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper


class AgentFirstContractBundlePythonRendererTest(unittest.TestCase):
    def test_rendered_wrapper_compiles(self) -> None:
        rendered = render_python_wrapper(
            '@pullwise/agent-task-contract',
            '0.1.0',
            '0' * 64,
            '1' * 64,
            b'{}',
        )

        compile(rendered, 'generated-wrapper.py', 'exec')


if __name__ == '__main__':
    unittest.main()
