IDENTITY_EVENTS = [
    {
        "anonymous": False,
        "type": "event",
        "name": "Registered",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "agentURI", "type": "string"},
            {"indexed": True, "name": "owner", "type": "address"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Transfer",
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": True, "name": "tokenId", "type": "uint256"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "URIUpdated",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": False, "name": "newURI", "type": "string"},
            {"indexed": True, "name": "updatedBy", "type": "address"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "MetadataSet",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "indexedMetadataKey", "type": "string"},
            {"indexed": False, "name": "metadataKey", "type": "string"},
            {"indexed": False, "name": "metadataValue", "type": "bytes"},
        ],
    },
]

REPUTATION_EVENTS = [
    {
        "anonymous": False,
        "type": "event",
        "name": "NewFeedback",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "clientAddress", "type": "address"},
            {"indexed": False, "name": "feedbackIndex", "type": "uint64"},
            {"indexed": False, "name": "value", "type": "int128"},
            {"indexed": False, "name": "valueDecimals", "type": "uint8"},
            {"indexed": True, "name": "indexedTag1", "type": "string"},
            {"indexed": False, "name": "tag1", "type": "string"},
            {"indexed": False, "name": "tag2", "type": "string"},
            {"indexed": False, "name": "endpoint", "type": "string"},
            {"indexed": False, "name": "feedbackURI", "type": "string"},
            {"indexed": False, "name": "feedbackHash", "type": "bytes32"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "FeedbackRevoked",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "clientAddress", "type": "address"},
            {"indexed": True, "name": "feedbackIndex", "type": "uint64"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "ResponseAppended",
        "inputs": [
            {"indexed": True, "name": "agentId", "type": "uint256"},
            {"indexed": True, "name": "clientAddress", "type": "address"},
            {"indexed": False, "name": "feedbackIndex", "type": "uint64"},
            {"indexed": True, "name": "responder", "type": "address"},
            {"indexed": False, "name": "responseURI", "type": "string"},
            {"indexed": False, "name": "responseHash", "type": "bytes32"},
        ],
    },
]

TRANSFER_EVENT = {
    "anonymous": False,
    "type": "event",
    "name": "Transfer",
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"},
    ],
}

AUTHORIZATION_USED_EVENT = {
    "anonymous": False,
    "type": "event",
    "name": "AuthorizationUsed",
    "inputs": [
        {"indexed": True, "name": "authorizer", "type": "address"},
        {"indexed": True, "name": "nonce", "type": "bytes32"},
    ],
}
